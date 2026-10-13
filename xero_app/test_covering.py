"""An administrator who covers a colleague's book (User.also_manages) and works
the Lawyers page (legal_access) - without being a Super Admin, and without
getting the legal updates."""
from unittest import mock

from django.urls import reverse

from . import legal_workflow
from django.core.files.uploadedfile import SimpleUploadedFile
from .models import DebtorNotice, LegalStep, LegalStepComment
from .tests import BookFixture, User


class CoveringTests(BookFixture):

    def setUp(self):
        super().setUp()
        # Ben covers Anna's book and works the Lawyers page - like Nthabiseng
        # covering Phumzile.
        self.ben.also_manages.add(self.anna)
        self.ben.legal_access = True
        self.ben.save()
        patcher = mock.patch("xero_app.notifications.send_app_email", return_value=1)
        self.email = patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(self.ben)

    def _names(self, r):
        return sorted(d["name"] for d in r.context["debtors"])

    def test_sees_their_own_and_the_covered_book(self):
        r = self.client.get(reverse("xero_aging_report"))
        # Ben's own (Riverside) plus Anna's (Karoo); Anna's others are in handover / with attorneys.
        self.assertEqual(self._names(r), ["Karoo Butchery", "Riverside Spar"])
        self.assertEqual(r.context["legal_accounts_count"], 1)        # Anna's attorney account is accounted for
        r = self.client.get(reverse("xero_aging_report"), {"admin": self.anna.id})
        self.assertEqual(self._names(r), ["Karoo Butchery"])
        r = self.client.get(reverse("xero_aging_report"), {"admin": self.boss.id})   # not someone Ben covers
        self.assertEqual(self._names(r), ["Karoo Butchery", "Riverside Spar"])

    def test_handover_page_and_dashboard_cover_both_books(self):
        r = self.client.get(reverse("xero_handover"))
        self.assertIn("Hilltop Store", [d["name"] for d in r.context["debtors"]])
        r = self.client.get(reverse("xero_dashboard"))
        self.assertEqual(str(r.context["my_total"]), "1800.00")          # Karoo 1,000 + Riverside 800
        self.assertEqual({a.id for a in r.context["admins"]}, {self.anna.id, self.ben.id})

    def test_reports_can_be_switched_to_the_covered_clerk_only(self):
        r = self.client.get(reverse("xero_reports"), {"clerk": self.anna.id})
        self.assertEqual(r.context["r"]["scope"].clerk, self.anna.id)
        r = self.client.get(reverse("xero_reports"), {"clerk": self.boss.id})
        self.assertEqual(r.context["r"]["scope"].clerk, self.ben.id)

    def test_works_the_lawyers_page(self):
        step = "receipt_instruction"
        self.client.post(reverse("xero_legal_step_toggle", args=[self.matter.id]), {"step_key": step})
        self.client.post(reverse("xero_legal_step_comment", args=[self.matter.id]), {
            "step_key": step, "text": "Statement attached",
            "documents": SimpleUploadedFile("s.pdf", b"%PDF-1.4", content_type="application/pdf")})
        self.assertTrue(LegalStep.objects.filter(matter=self.matter, step_key=step, done=True).exists())
        self.assertTrue(LegalStepComment.objects.filter(matter=self.matter, text="Statement attached").exists())

    def test_no_super_admin_powers(self):
        self.assertEqual(self.client.get(reverse("xero_categories")).status_code, 403)
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 403)
        self.client.post(reverse("xero_legal_approve"), {"matter_id": self.matter.id})
        self.client.post(reverse("xero_allocate_debtor"), {"contact_id": "c-closed", "administrator": self.ben.id})
        self.assertFalse(self.matter.__class__.objects.filter(id=self.matter.id, status="closed").exists())

    def test_does_not_get_legal_updates(self):
        lawyer = User.objects.create_user("law@fsa.test", "x", role="lawyer")
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_step_comment", args=[self.matter.id]), {
            "step_key": legal_workflow.LOD_HAND_DELIVERY, "confirm_delivery": "1",
            "documents": SimpleUploadedFile("r.pdf", b"%PDF-1.4", content_type="application/pdf")})
        told = set(DebtorNotice.objects.values_list("recipient__email", flat=True))
        self.assertIn(lawyer.email, told)
        self.assertNotIn(self.ben.email, told)
        emailed = {a for c in self.email.call_args_list for a in c.kwargs["to"]}
        self.assertNotIn(self.ben.email, emailed)

    def test_set_on_the_users_page(self):
        self.client.force_login(self.boss)
        page = self.client.get(reverse("user_edit", args=[self.ben.id]))
        self.assertContains(page, "Can also manage the debtors of")
        self.client.post(reverse("user_edit", args=[self.ben.id]), {
            "first_name": "Ben", "last_name": "Clerk", "email": self.ben.email, "role": "administrator",
            "legal_access": "on", "also_manages": [self.anna.id], "is_active": "on"})
        self.assertEqual(list(self.ben.also_manages.values_list("id", flat=True)), [self.anna.id])
