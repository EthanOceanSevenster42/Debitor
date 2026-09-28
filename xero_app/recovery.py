"""Admin collections-recovery detection.

Each sync rebuilds the open-invoice snapshot. By capturing the previous snapshot
first and comparing amount_due, we can see money coming in: a drop in amount_due
(or an invoice dropping off the open list entirely) is a payment. Each payment is
logged as a RecoveredInvoice — partial payments each get their own row.

Attribution (agreed rules), at the time of payment:
  * COLLECTIONS — the invoice was still being chased (allocated + a logged
    Call/WhatsApp/Email) and had NOT yet reached handover. Credited to the
    allocated ADMINISTRATOR. -> REASON_COLLECTED.
  * WITH THE LAWYERS — the company had an APPROVED (active) legal matter when the
    payment landed. Counts as recovered money but the credit goes to the LAWYERS
    (a team total; no administrator is attributed). -> REASON_COLLECTED_LEGAL.
    Each payment counts (partial payments included).
Payments that land while only on the handover page (sent but not yet approved with
the lawyers), or with no allocation, are recorded but left uncredited.

This is pure database work (no Xero API calls), so it never affects the sync's
rate budget, and it is designed to never raise into the sync — call it inside a
try/except in the sync command.
"""

from decimal import Decimal

from .models import (OpenInvoiceSnapshot, WriteOffInvoice, HandoverInvoice,
                     HandoverSetting, DebtorAllocation, CallLog, LegalMatter,
                     RecoveredInvoice, DEFAULT_HANDOVER_DAYS)


def capture_open_snapshot(tenant_id):
    """Snapshot the current open invoices for later delta comparison.

    Call this BEFORE the sync rebuilds the snapshot. Returns
    {invoice_id: {amount_due, contact_id, contact_name, invoice_number, days_past_due}}.
    """
    return {
        s["invoice_id"]: s
        for s in OpenInvoiceSnapshot.objects.filter(tenant_id=tenant_id).values(
            "invoice_id", "amount_due", "contact_id", "contact_name",
            "invoice_number", "days_past_due")
    }


def _threshold_map(tenant_id):
    """contact_id -> auto-handover threshold days, or None for 'never'."""
    out = {}
    for hs in HandoverSetting.objects.filter(tenant_id=tenant_id).values(
            "contact_id", "auto_handover", "handover_days"):
        out[hs["contact_id"]] = hs["handover_days"] if hs["auto_handover"] else None
    return out


def _reached_handover(contact_id, invoice_id, days_past_due, threshold_map,
                      handover_ids, legal_contacts):
    """Whether the invoice had reached handover / the lawyers at payment time —
    in which case the admin no longer earns credit for it."""
    if invoice_id in handover_ids:          # manually marked / on the handover page
        return True
    if contact_id in legal_contacts:        # company sent to the lawyers
        return True
    threshold = threshold_map.get(contact_id, DEFAULT_HANDOVER_DAYS)
    return threshold is not None and (days_past_due or 0) >= threshold


def detect_recoveries(tenant_id, prev_snapshot):
    """Compare prev_snapshot (captured before the rebuild) with the freshly-built
    open snapshot and log a RecoveredInvoice for each detected payment. Returns the
    number of payment rows created."""
    if not prev_snapshot:
        return 0
    new_due = dict(OpenInvoiceSnapshot.objects.filter(tenant_id=tenant_id)
                   .values_list("invoice_id", "amount_due"))
    # Guard against a partial/failed fetch making every invoice look "paid": if the
    # whole open list vanished, don't treat that as a wave of recoveries.
    if not new_due:
        return 0

    writeoff_ids = set(WriteOffInvoice.objects.filter(tenant_id=tenant_id)
                       .values_list("invoice_id", flat=True))
    handover_ids = set(HandoverInvoice.objects.filter(tenant_id=tenant_id)
                       .values_list("invoice_id", flat=True))
    # All matters mark "reached handover/lawyers" (no collections credit); only
    # APPROVED (active) matters earn the new legal-stage recovery credit.
    legal_contacts = set(LegalMatter.objects.filter(tenant_id=tenant_id)
                         .values_list("contact_id", flat=True))
    active_legal_contacts = set(LegalMatter.objects
                                .filter(tenant_id=tenant_id, status=LegalMatter.ACTIVE)
                                .values_list("contact_id", flat=True))
    threshold_map = _threshold_map(tenant_id)
    alloc = {a.contact_id: a.administrator
             for a in DebtorAllocation.objects.filter(tenant_id=tenant_id).select_related("administrator")}
    contacted_ids = set(CallLog.objects.filter(tenant_id=tenant_id)
                        .values_list("invoice_id", flat=True))

    rows = []
    for iid, p in prev_snapshot.items():
        prev_due = p["amount_due"] or Decimal(0)
        cur = new_due.get(iid)
        if cur is None:
            # Left the open list. A write-off isn't money in; otherwise it settled.
            if iid in writeoff_ids:
                continue
            delta = prev_due
        else:
            delta = prev_due - (cur or Decimal(0))
        if delta <= 0:
            continue

        cid = p["contact_id"] or ""
        dpd = p["days_past_due"] or 0
        admin = alloc.get(cid)
        followed_up = iid in contacted_ids

        if cid in active_legal_contacts:
            # Recovered while approved/active with the lawyers. This counts as
            # recovered money (credited) but the credit goes to the LAWYERS, not
            # the administrator — so no admin is attributed to the row.
            credited = True
            reason = RecoveredInvoice.REASON_COLLECTED_LEGAL
            credit_admin = None
        elif _reached_handover(cid, iid, dpd, threshold_map, handover_ids, legal_contacts):
            # On the handover page (or a pending/closed matter) but not active with
            # the lawyers — recorded, but nobody earns collections credit.
            credited = False
            reason = RecoveredInvoice.REASON_HANDOVER_LEGAL
            credit_admin = None
        else:
            # Still in active collections — credit the allocated admin.
            credited = bool(admin) and followed_up
            credit_admin = admin if credited else None
            reason = (RecoveredInvoice.REASON_COLLECTED if credited
                      else (RecoveredInvoice.REASON_UNALLOCATED if not admin
                            else RecoveredInvoice.REASON_NO_FOLLOWUP))

        rows.append(RecoveredInvoice(
            tenant_id=tenant_id, invoice_id=iid,
            invoice_number=p.get("invoice_number") or "",
            contact_id=cid, contact_name=p.get("contact_name") or "",
            amount=delta, credited=credited,
            administrator=credit_admin,
            administrator_name=(credit_admin.get_full_name() or credit_admin.email) if credit_admin else "",
            # Whose portfolio the money came in on, whether or not it counts as
            # their collection - what the reports total against each clerk.
            allocated_admin=admin,
            allocated_admin_name=(admin.get_full_name() or admin.email) if admin else "",
            reason=reason, days_past_due=dpd,
        ))

    if rows:
        RecoveredInvoice.objects.bulk_create(rows, batch_size=500)
    return len(rows)
