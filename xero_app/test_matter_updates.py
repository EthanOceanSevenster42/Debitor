"""Both sides hear about a legal matter without having to keep checking it:
the attorneys when FSA hands over, comments, uploads or changes something, and
FSA when the attorneys do."""
import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from . import legal_workflow
from .models import DebtorNotice, LegalMatter
from .tests import TENANT, BookFixture, User

_MEDIA = tempfile.mkdtemp(prefix="fsa-test-media-")
STEP = "receipt_instruction"


@override_settings(MEDIA_ROOT=_MEDIA)
class MatterUpdateNotificationTests(BookFixture):

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.bertus = User.objects.create_user("bertus@law.test", "x", role="lawyer", first_name="Bertus")
        self.devan = User.objects.create_user("devan@law.test", "x", role="lawyer", first_name="Devan")
        self.silje = User.objects.create_user("silje@law.test", "x", role="lawyer", first_name="Silje")
        patcher = mock.patch("xero_app.notifications.send_app_email", return_value=1)
        self.email = patcher.start()
        self.addCleanup(patcher.stop)

    def _emailed(self):
        """Everyone emailed so far - each person gets their own copy."""
        return {addr for call in self.email.call_args_list for addr in call.kwargs["to"]}

    def _told(self, kind=DebtorNotice.KIND_MATTER_UPDATE):
        return set(DebtorNotice.objects.filter(kind=kind).values_list("recipient__email", flat=True))

    LEGAL = {"bertus@law.test", "devan@law.test", "silje@law.test"}

    def test_a_new_instruction_reaches_the_attorneys(self):
        pending = LegalMatter.objects.create(tenant_id=TENANT, contact_id="c-ho", contact_name="Hilltop",
                                             status=LegalMatter.PENDING)
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_approve"), {"matter_id": pending.id})
        self.assertEqual(self._told(DebtorNotice.KIND_NEW_MATTER), self.LEGAL)
        self.assertTrue(self.LEGAL <= self._emailed())

    def test_fsa_uploading_documents_tells_the_attorneys(self):
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_step_comment", args=[self.matter.id]), {
            "step_key": STEP, "nature": "Statement",
            "documents": SimpleUploadedFile("statement.pdf", b"%PDF-1.4", content_type="application/pdf")})
        self.assertEqual(self._told(), self.LEGAL)
        self.assertEqual(self._emailed(), self.LEGAL)
        notice = DebtorNotice.objects.filter(kind=DebtorNotice.KIND_MATTER_UPDATE).first()
        self.assertIn("uploaded 1 document", notice.text)
        self.assertIn("statement.pdf (Statement)", notice.text)
        self.assertEqual(notice.matter, self.matter)

    def test_fsa_commenting_tells_the_attorneys(self):
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_step_comment", args=[self.matter.id]),
                         {"step_key": STEP, "text": "Debtor called, wants to settle"})
        self.assertEqual(self._told(), self.LEGAL)
        self.assertIn("Debtor called, wants to settle",
                      DebtorNotice.objects.filter(kind=DebtorNotice.KIND_MATTER_UPDATE).first().text)

    def test_the_attorneys_commenting_uploading_or_updating_tells_fsa(self):
        self.client.force_login(self.bertus)
        self.client.post(reverse("xero_legal_step_comment", args=[self.matter.id]), {
            "step_key": STEP, "text": "Summons drafted",
            "documents": SimpleUploadedFile("summons.pdf", b"%PDF-1.4", content_type="application/pdf")})
        self.client.post(reverse("xero_legal_step_toggle", args=[self.matter.id]), {"step_key": STEP})
        self.client.post(reverse("xero_legal_toggle_opposed", args=[self.matter.id]), {"which": "summons"})
        texts = list(DebtorNotice.objects.filter(kind=DebtorNotice.KIND_MATTER_UPDATE)
                     .values_list("text", flat=True))
        # FSA (the Super Admin) is told about all three; the other attorneys are not.
        self.assertEqual(self._told(), {self.boss.email})
        self.assertEqual(len(texts), 3)
        self.assertTrue(any('marked "Receipt of Instruction" as done' in t for t in texts))
        self.assertTrue(any("changed Summons to Opposed" in t for t in texts))
        self.assertEqual(self._emailed(), {self.boss.email})

    def test_nobody_is_told_about_their_own_change(self):
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_step_toggle", args=[self.matter.id]), {"step_key": STEP})
        self.assertNotIn(self.boss.email, self._told())

    def test_closing_or_bringing_back_a_matter_tells_the_attorneys(self):
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_return"), {"matter_id": self.matter.id, "note": "Paid in full"})
        notice = DebtorNotice.objects.filter(kind=DebtorNotice.KIND_MATTER_UPDATE).first()
        self.assertEqual(self._told(), self.LEGAL)
        self.assertIn("brought the matter back", notice.text)
        self.assertIn("Paid in full", notice.text)

    def test_the_lod_delivery_confirmation_is_not_sent_twice(self):
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_step_comment", args=[self.matter.id]), {
            "step_key": legal_workflow.LOD_HAND_DELIVERY, "confirm_delivery": "1",
            "documents": SimpleUploadedFile("receipt.pdf", b"%PDF-1.4", content_type="application/pdf")})
        self.assertEqual(self._told(), set())          # the dedicated "LOD delivered" notice covers it
        self.assertTrue(self._told(DebtorNotice.KIND_LOD_DELIVERED))
