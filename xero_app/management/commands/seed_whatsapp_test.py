"""Seed a test debtor whose WhatsApp number is your own, to prove a real send.

The WhatsApp button sends from the server now, so the only way to check that the
whole chain works — approved template, WATI account, number formatting, the
CallLog that follows a successful send — is to point one debtor at a handset you
are holding. This creates a self-contained one-invoice debtor for that, so no
real customer's number is touched and no real reminder can go to the wrong phone.

Run:  python manage.py seed_whatsapp_test
      python manage.py seed_whatsapp_test --number 0821234567
      python manage.py seed_whatsapp_test --remove

Re-runnable: the debtor has fixed ids, so running it again updates in place.

Note the row lives in OpenInvoiceSnapshot, which sync_xero rebuilds from Xero on
every run — the test debtor disappears at the next sync. Re-run this to get it
back.
"""
import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from xero_app.models import (XeroConnection, OpenInvoiceSnapshot, ContactDetail,
                             MessageTemplate)
from xero_app.views import _bucket_for, _pick_whatsapp_number, _wa_format_phone

# Fixed so the command is re-runnable and the row is obvious in the database.
# The 'TEST-' prefixes also make it unmistakable on the Debtors Action page.
TEST_CONTACT_ID = "TEST-WHATSAPP-CONTACT"
TEST_INVOICE_ID = "TEST-WHATSAPP-INVOICE"
TEST_INVOICE_NUMBER = "TEST-WA-001"
TEST_NAME = "WhatsApp Test Debtor"
DEFAULT_NUMBER = "0716254982"


class Command(BaseCommand):
    help = "Seed a test debtor with a WhatsApp number you control, for testing sends."

    def add_arguments(self, parser):
        parser.add_argument("--number", default=DEFAULT_NUMBER,
                            help=f"WhatsApp number to reach (default {DEFAULT_NUMBER}).")
        parser.add_argument("--name", default=TEST_NAME,
                            help="Debtor name shown on the Debtors Action page.")
        parser.add_argument("--amount", default="1.00",
                            help="Amount due on the test invoice (default 1.00).")
        parser.add_argument("--days-overdue", type=int, default=45,
                            help="How overdue the test invoice looks (default 45).")
        parser.add_argument("--tenant", default=None,
                            help="Tenant id to seed into (default: the first connection).")
        parser.add_argument("--remove", action="store_true",
                            help="Delete the test debtor instead of creating it.")

    def handle(self, *args, **opts):
        tid = opts["tenant"]
        if not tid:
            conn = XeroConnection.objects.first()
            if not conn:
                self.stderr.write("No XeroConnection — connect to Xero first.")
                return
            tid = conn.tenant_id

        if opts["remove"]:
            invoices, _ = OpenInvoiceSnapshot.objects.filter(
                tenant_id=tid, invoice_id=TEST_INVOICE_ID).delete()
            contacts, _ = ContactDetail.objects.filter(
                tenant_id=tid, contact_id=TEST_CONTACT_ID).delete()
            self.stdout.write(self.style.SUCCESS(
                f"Removed the test debtor ({invoices} invoice, {contacts} contact record)."))
            return

        number = _wa_format_phone(opts["number"])
        if not number:
            self.stderr.write(f"'{opts['number']}' has no usable digits.")
            return

        name = opts["name"]
        days = opts["days_overdue"]
        today = timezone.now().date()
        due = today - timedelta(days=days)

        # ContactDetail is what _pick_whatsapp_number reads, so the number has to
        # land in `phones` as a Mobile entry — that is the one it prefers.
        ContactDetail.objects.update_or_create(
            tenant_id=tid, contact_id=TEST_CONTACT_ID,
            defaults={"data_json": json.dumps({
                "name": name,
                "account_number": "FSA-TEST",
                "email": "whatsapp-test@example.invalid",
                "status": "ACTIVE",
                "default_currency": "ZAR",
                "phones": [{"type": "Mobile", "number": opts["number"]}],
                "addresses": [{"type": "Street",
                               "lines": ["Test record — not a real customer"]}],
                "contact_persons": [],
                "payment_terms": "30 days after invoice date",
            })},
        )

        OpenInvoiceSnapshot.objects.update_or_create(
            tenant_id=tid, invoice_id=TEST_INVOICE_ID,
            defaults={
                "invoice_number": TEST_INVOICE_NUMBER,
                "contact_id": TEST_CONTACT_ID,
                "contact_name": name,
                "contact_email": "whatsapp-test@example.invalid",
                "invoice_date": due - timedelta(days=30),
                "due_date": due,
                "days_past_due": days,
                "bucket": _bucket_for(days),
                "amount_due": opts["amount"],
                "total": opts["amount"],
                "currency": "ZAR",
                "status": "AUTHORISED",
                "project_code": "",
                "inspector": "",
                "online_url": "",
            },
        )

        stored = ContactDetail.objects.get(tenant_id=tid, contact_id=TEST_CONTACT_ID)
        resolved = _pick_whatsapp_number(json.loads(stored.data_json))
        self.stdout.write(self.style.SUCCESS(
            f"Seeded '{name}' with invoice {TEST_INVOICE_NUMBER} "
            f"(R {opts['amount']}, {days} days overdue)."))
        self.stdout.write(f"WhatsApp will send to +{resolved} (from {opts['number']}).")

        # A send needs a template mapped to an approved WhatsApp template; without
        # one the button is shown disabled, which is easy to mistake for a bug.
        mapped = MessageTemplate.objects.filter(
            channel=MessageTemplate.CHANNEL_WHATSAPP).exclude(wati_template_name="")
        if mapped.exists():
            names = ", ".join(f"{t.name} -> {t.wati_template_name}" for t in mapped)
            self.stdout.write(f"Sendable templates: {names}")
        else:
            self.stdout.write(self.style.WARNING(
                "No WhatsApp template is linked to an approved WhatsApp template yet — "
                "the button will show disabled until one is linked in Communication Setup."))
        self.stdout.write("Open the Debtors Action page and search for the test debtor. "
                          "The next Xero sync clears this row; re-run to restore it.")
