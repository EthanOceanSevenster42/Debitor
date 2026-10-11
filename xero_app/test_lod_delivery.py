"""The 'Physical delivery of LOD' milestone: due 7 days after the Letter of
Demand is sent, FSA alerted once, completed only with signed proof uploaded,
and everyone told when it is done."""
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from . import legal_workflow, notifications
from .models import DebtorNotice, LegalMatter, LegalStep
from .tests import BookFixture, User

_MEDIA = tempfile.mkdtemp(prefix="fsa-test-media-")
HAND = legal_workflow.LOD_HAND_DELIVERY


@override_settings(MEDIA_ROOT=_MEDIA)
class LodHandDeliveryTests(BookFixture):

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.lawyer = User.objects.create_user("law@fsa.test", "x", role="lawyer")
        patcher = mock.patch("xero_app.notifications.send_app_email", return_value=1)
        self.email = patcher.start()
        self.addCleanup(patcher.stop)

    def _send_lod(self, days_ago):
        LegalStep.objects.create(matter=self.matter, step_key="lod_email", done=True,
                                 done_at=timezone.now() - timedelta(days=days_ago))

    def test_it_is_a_collections_step_straight_after_service_of_the_lod(self):
        keys = [k for k, _l, _v in legal_workflow.COLLECTIONS]
        self.assertEqual(keys.index(HAND), keys.index("lod_sheriff") + 1)

    def test_fsa_is_alerted_once_seven_days_after_the_lod_was_sent(self):
        self._send_lod(3)
        self.assertEqual(notifications.send_due_lod_delivery_reminders(), 0)   # not yet
        LegalStep.objects.filter(matter=self.matter).update(done_at=timezone.now() - timedelta(days=8))
        self.assertEqual(notifications.send_due_lod_delivery_reminders(), 1)
        notice = DebtorNotice.objects.get(kind=DebtorNotice.KIND_LOD_DELIVERY_DUE)
        self.assertEqual((notice.recipient, notice.matter), (self.boss, self.matter))
        self.assertIn("FSA inspector", notice.text)
        self.assertTrue(self.email.called)
        self.assertIsNotNone(LegalMatter.objects.get(id=self.matter.id).lod_delivery_notified_at)
        self.assertEqual(notifications.send_due_lod_delivery_reminders(), 0)   # only once

    def test_it_cannot_be_ticked_without_the_signed_proof(self):
        self._send_lod(8)
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_step_toggle", args=[self.matter.id]), {"step_key": HAND})
        self.assertFalse(LegalStep.objects.filter(matter=self.matter, step_key=HAND, done=True).exists())
        self.client.post(reverse("xero_legal_step_comment", args=[self.matter.id]),
                         {"step_key": HAND, "confirm_delivery": "1", "text": "Delivered"})
        self.assertFalse(LegalStep.objects.filter(matter=self.matter, step_key=HAND, done=True).exists())

    def test_uploading_the_proof_completes_it_and_tells_fsa_and_the_legal_team(self):
        self._send_lod(8)
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_step_comment", args=[self.matter.id]), {
            "step_key": HAND, "confirm_delivery": "1", "nature": "Signed proof of delivery",
            "text": "Delivered by inspector, signed by the manager",
            "documents": SimpleUploadedFile("receipt.pdf", b"%PDF-1.4", content_type="application/pdf")})
        step = LegalStep.objects.get(matter=self.matter, step_key=HAND)
        self.assertTrue(step.done)
        told = set(DebtorNotice.objects.filter(kind=DebtorNotice.KIND_LOD_DELIVERED)
                   .values_list("recipient__email", flat=True))
        self.assertIn(self.lawyer.email, told)          # the legal team
        self.assertNotIn(self.boss.email, told)         # not the person who confirmed it
        self.assertTrue(self.email.called)
        state = legal_workflow.lod_delivery_state(
            LegalMatter.objects.prefetch_related("step_states").get(id=self.matter.id), timezone.now())
        self.assertEqual((state["done"], state["outstanding"]), (True, False))

    def test_outstanding_delivery_shows_on_the_pages_until_done(self):
        self._send_lod(9)
        self.client.force_login(self.boss)
        page = self.client.get(reverse("xero_legal_matter", args=[self.matter.id]))
        self.assertContains(page, "Outstanding since")
        self.assertContains(page, "Upload proof &amp; confirm delivered")
        legal = self.client.get(reverse("xero_legal"))
        self.assertContains(legal, "Hand delivery outstanding since")
        report = self.client.get(reverse("xero_reports")).context["r"]
        reasons = {e["cid"]: {k for k, _ in e["reasons"]} for e in report["escalations"]}
        self.assertIn("lod_delivery", reasons["c-legal"])

    def test_the_notification_opens_onto_the_matter(self):
        self._send_lod(8)
        notifications.send_due_lod_delivery_reminders()
        self.client.force_login(self.boss)
        page = self.client.get(reverse("xero_notifications"))
        self.assertContains(page, "LOD hand delivery due")
        self.assertContains(page, reverse("xero_legal_matter", args=[self.matter.id]))
