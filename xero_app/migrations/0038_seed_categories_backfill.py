"""Seed the starting debtor categories and back-fill the new reporting fields.

* Categories: the classifications finance asked to report on. Abattoirs and
  training accounts can usually be recognised from the name or project code,
  so they get keywords; stores cannot, so they are classified by hand.
* RecoveredInvoice.allocated_admin: who held each debtor when the payment came
  in. Credited rows already name them; for the rest, the allocation as it stands
  now is the best record there is.
* WriteOffInvoice.amount: what was owed when written off, from the open-invoice
  snapshot where the invoice is still open in Xero.
"""
from django.db import migrations

CATEGORIES = [
    ("Abattoirs", "abattoir", 10),
    ("Corporate stores", "", 20),
    ("Individual stores", "", 30),
    ("Training", "training", 40),
    ("Other", "", 90),
]


def forwards(apps, schema_editor):
    DebtorCategory = apps.get_model("xero_app", "DebtorCategory")
    for name, keywords, order in CATEGORIES:
        DebtorCategory.objects.get_or_create(
            name=name, defaults={"keywords": keywords, "sort_order": order,
                                 "updated_by": "system"})

    User = apps.get_model("accounts", "User")
    DebtorAllocation = apps.get_model("xero_app", "DebtorAllocation")
    RecoveredInvoice = apps.get_model("xero_app", "RecoveredInvoice")
    names = {u.id: ((f"{u.first_name} {u.last_name}".strip()) or u.email)
             for u in User.objects.all()}
    alloc = {(a.tenant_id, a.contact_id): a.administrator_id
             for a in DebtorAllocation.objects.all()}
    for r in RecoveredInvoice.objects.filter(allocated_admin__isnull=True):
        admin_id = r.administrator_id or alloc.get((r.tenant_id, r.contact_id))
        if not admin_id and not r.contact_id:
            continue
        if not admin_id:
            # Recoveries store the Xero contact id; an id-less debtor is
            # allocated under its name.
            admin_id = alloc.get((r.tenant_id, r.contact_name))
        if admin_id:
            r.allocated_admin_id = admin_id
            r.allocated_admin_name = names.get(admin_id, "")
            r.save(update_fields=["allocated_admin", "allocated_admin_name"])

    WriteOffInvoice = apps.get_model("xero_app", "WriteOffInvoice")
    OpenInvoiceSnapshot = apps.get_model("xero_app", "OpenInvoiceSnapshot")
    owed = {(s.tenant_id, s.invoice_id): s.amount_due
            for s in OpenInvoiceSnapshot.objects.all().only("tenant_id", "invoice_id", "amount_due")}
    for w in WriteOffInvoice.objects.filter(amount=0):
        amt = owed.get((w.tenant_id, w.invoice_id))
        if amt:
            w.amount = amt
            w.save(update_fields=["amount"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_remove_auditlog_ip_address"),
        ("xero_app", "0037_reporting_categories_snapshots"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
