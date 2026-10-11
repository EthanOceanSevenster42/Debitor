"""Client details for legal proceedings: captured on the matter, pre-filled from
Xero, checked for obvious typos, and the other side told what changed."""
import json
from unittest import mock

from django.urls import reverse

from .forms import valid_sa_id
from .models import ContactDetail, DebtorNotice, LegalClientInfo
from .tests import TENANT, BookFixture, User

GOOD = {"registered_name": "Garden Route Abattoir (Pty) Ltd", "entity_type": "company",
        "registration_number": "2015/123456/07", "id_number": "", "vat_number": "4123456789",
        "directors": "J. Smith (8001015009087)", "domicilium_address": "1 Main Rd, George",
        "postal_address": "", "contact_person": "J. Smith", "contact_phone": "082 123 4567",
        "contact_email": "accounts@gra.test", "notes": "", "trading_name": ""}


class ClientInfoTests(BookFixture):

    def setUp(self):
        super().setUp()
        self.bertus = User.objects.create_user("bertus@law.test", "x", role="lawyer")
        patcher = mock.patch("xero_app.notifications.send_app_email", return_value=1)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = reverse("xero_legal_client_info", args=[self.matter.id])

    def test_sa_id_checksum(self):
        self.assertTrue(valid_sa_id("8001015009087"))
        self.assertFalse(valid_sa_id("8001015009086"))
        self.assertFalse(valid_sa_id("12345"))

    def test_the_form_starts_with_what_xero_knows(self):
        ContactDetail.objects.create(tenant_id=TENANT, contact_id="c-legal", data_json=json.dumps({
            "name": "Garden Route Abattoir (Pty) Ltd", "email": "accounts@gra.test", "tax_number": "4123456789",
            "phones": [{"type": "Mobile", "number": "082 123 4567"}],
            "addresses": [{"type": "Street", "lines": ["1 Main Rd", "George"]},
                          {"type": "Pobox", "lines": ["PO Box 9", "George"]}],
            "contact_persons": [{"name": "J. Smith", "email": "js@gra.test"}]}))
        self.client.force_login(self.boss)
        page = self.client.get(reverse("xero_legal_matter", args=[self.matter.id]))
        form = page.context["client_form"]
        self.assertEqual(form.initial["vat_number"], "4123456789")
        self.assertEqual(form.initial["domicilium_address"], "1 Main Rd\nGeorge")
        self.assertEqual(form.initial["postal_address"], "PO Box 9\nGeorge")
        self.assertEqual(form.initial["contact_person"], "J. Smith")
        self.assertContains(page, "Pre-filled from Xero")
        self.assertFalse(LegalClientInfo.objects.exists())        # nothing saved until someone checks it

    def test_saving_stores_the_details_and_tells_the_attorneys_what_changed(self):
        self.client.force_login(self.boss)
        self.client.post(self.url, dict(GOOD, id_number="800101 5009 087"))
        info = LegalClientInfo.objects.get(matter=self.matter)
        self.assertEqual((info.registration_number, info.id_number), ("2015/123456/07", "8001015009087"))
        self.assertEqual(info.updated_by, self.boss.email)
        notice = DebtorNotice.objects.get(kind=DebtorNotice.KIND_MATTER_UPDATE, recipient=self.bertus)
        self.assertIn("updated the client details", notice.text)
        self.assertIn("ID number", notice.text)
        page = self.client.get(reverse("xero_legal_matter", args=[self.matter.id]))
        self.assertContains(page, "2015/123456/07")
        # Saving again unchanged is not news.
        self.client.post(self.url, dict(GOOD, id_number="8001015009087"))
        self.assertEqual(DebtorNotice.objects.filter(kind=DebtorNotice.KIND_MATTER_UPDATE).count(), 1)

    def test_obvious_typos_are_caught_and_nothing_is_saved(self):
        self.client.force_login(self.boss)
        page = self.client.post(self.url, dict(GOOD, id_number="8001015009086", vat_number="123",
                                               contact_email="not-an-email"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "not a valid 13-digit South African ID number")
        self.assertContains(page, "10 digits and starts with 4")
        self.assertContains(page, "Enter a valid email address")
        self.assertFalse(LegalClientInfo.objects.exists())

    def test_attorneys_can_edit_and_clerks_cannot(self):
        self.client.force_login(self.bertus)
        self.client.post(self.url, GOOD)
        self.assertTrue(LegalClientInfo.objects.exists())
        self.assertTrue(DebtorNotice.objects.filter(kind=DebtorNotice.KIND_MATTER_UPDATE,
                                                    recipient=self.boss).exists())
        self.client.force_login(self.anna)
        self.client.post(self.url, dict(GOOD, registration_number="2020/999999/07"))
        self.assertEqual(LegalClientInfo.objects.get().registration_number, "2015/123456/07")
        page = self.client.get(reverse("xero_legal_matter", args=[self.matter.id]))
        self.assertNotContains(page, "Edit details")
