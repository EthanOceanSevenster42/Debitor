"""Someone who processes the write-offs of a project group (Training, IMI, APS)
sees those written-off invoices in every book and can mark their credit notes."""
from decimal import Decimal

from django.urls import reverse

from .models import DebtorAllocation, WriteOffInvoice
from .tests import TENANT, BookFixture, User, _snap


class WriteOffGroupTests(BookFixture):

    def setUp(self):
        super().setUp()
        # Written off, in other people's books: training, IMI (unallocated) and APS.
        for iid, cid, name, project, admin in (
                ("wo-t1", "c-train", "Academy Training", "080 - Training", self.ben),
                ("wo-i1", "c-imi", "Karoo Abattoir", "010 - Red Meat", None),
                ("wo-a1", "c-aps", "Spar Delmas", "060 - Processed Meat Products", self.ben)):
            _snap(iid, cid, name, 120, "500", project=project)
            WriteOffInvoice.objects.create(tenant_id=TENANT, invoice_id=iid, contact_id=cid, amount=Decimal("500"))
            if admin:
                DebtorAllocation.objects.create(tenant_id=TENANT, contact_id=cid, administrator=admin)
        self.nth = User.objects.create_user("nth@fsa.test", "x", role="administrator", first_name="Nthabiseng",
                                            write_off_groups="training,imi")

    def _names(self, user, url_name="xero_write_offs"):
        self.client.force_login(user)
        return sorted(d["name"] for d in self.client.get(reverse(url_name)).context["debtors"])

    def test_the_groups_show_on_the_write_offs_page_from_every_book(self):
        self.assertEqual(self.nth.write_off_codes(), {"080", "010", "020"})
        self.assertEqual(self._names(self.nth), ["Academy Training", "Karoo Abattoir"])
        # Without the setting, an administrator sees only their own book's write-offs.
        self.nth.write_off_groups = ""
        self.nth.save()
        self.assertEqual(self._names(self.nth), [])

    def test_it_does_not_widen_any_other_page(self):
        _snap("wo-t2", "c-train", "Academy Training", 10, "300", project="080 - Training")
        self.assertNotIn("Academy Training", self._names(self.nth, "xero_aging_report"))

    def test_credit_notes_only_for_the_groups(self):
        self.client.force_login(self.nth)
        url = reverse("xero_credit_note_toggle")
        hdr = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        self.assertEqual(self.client.post(url, {"invoice_id": "wo-t1"}, **hdr).status_code, 200)
        self.assertTrue(WriteOffInvoice.objects.get(invoice_id="wo-t1").credit_note_issued)
        self.assertEqual(self.client.post(url, {"invoice_id": "wo-a1"}, **hdr).status_code, 403)
        self.assertFalse(WriteOffInvoice.objects.get(invoice_id="wo-a1").credit_note_issued)

    def test_the_tick_shows_on_the_invoice(self):
        self.client.force_login(self.nth)
        r = self.client.get(reverse("xero_debtor_statement"), {"cid": "c-train", "scope": "writeoff"})
        self.assertContains(r, "Issue Credit Note")
        self.assertNotContains(r, "Move back")                     # reversing stays with Super Admins

    def test_a_super_admin_sets_it_on_the_users_page(self):
        self.client.force_login(self.boss)
        self.assertContains(self.client.get(reverse("user_edit", args=[self.anna.id])),
                            "Can also see and process the write-offs for")
        self.client.post(reverse("user_edit", args=[self.anna.id]), {
            "first_name": "Anna", "last_name": "Clerk", "email": "anna@fsa.test", "role": "administrator",
            "write_off_groups": ["training", "imi"], "is_active": "on"})
        self.assertEqual(User.objects.get(id=self.anna.id).write_off_groups, "training,imi")
