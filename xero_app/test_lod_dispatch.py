"""Sending the Letter of Demand to an inspector for hand delivery: the email and
its attachments, the record kept, the inspector's upload link, and reminders."""
import shutil
import tempfile
from datetime import date, timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from . import legal_board, lod_dispatch, notifications
from .models import (DebtorNotice, Inspector, LegalMatter, LegalStep, LegalStepComment,
                     LegalStepCommentAttachment, LodInstruction, OpenInvoiceSnapshot)
from .statement_pdf import build_statement_pdf
from .tests import BookFixture, User

_MEDIA = tempfile.mkdtemp(prefix="fsa-lod-media-")


@override_settings(MEDIA_ROOT=_MEDIA)
class LodDispatchTests(BookFixture):

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.boss.first_name, self.boss.last_name = "Nicole", "Bergh"
        self.boss.save()
        self.lawyer = User.objects.create_user("law@fsa.test", "x", role="lawyer", first_name="Bertus")
        OpenInvoiceSnapshot.objects.filter(contact_id="c-legal").update(inspector="020 - S Dlisani")
        OpenInvoiceSnapshot.objects.filter(invoice_id="l2").update(inspector="020 - S Dlisani, 050 - L Maqina")
        LegalStep.objects.create(matter=self.matter, step_key="lod_email", done=True,
                                 done_at=timezone.now() - timedelta(days=8))
        self.docs = {name: self._upload(step, name) for step, name in (
            ("draft_lod", "Letter of Demand_Cover.pdf"), ("draft_lod", "Letter of Demand_Annexures (1-7).pdf"),
            ("lod_email", "POS.pdf"), ("lod_email", "Read Receipt.pdf"),
            ("draft_lod", "RE LETTER OF DEMAND FOOD SAFETY AGENCY.msg"))}
        for target in ("xero_app.lod_dispatch.send_app_email", "xero_app.notifications.send_app_email"):
            p = mock.patch(target, return_value=1)
            setattr(self, "mail" if "lod_dispatch" in target else "notify_mail", p.start())
            self.addCleanup(p.stop)
        p = mock.patch("xero_app.xero_client.fetch_invoice_pdf", side_effect=lambda conn, iid: b"%PDF invoice " + iid.encode())
        self.xero_pdf = p.start()
        self.addCleanup(p.stop)

    def _upload(self, step, name, data=b"%PDF-1.4 lod"):
        c = LegalStepComment.objects.create(matter=self.matter, step_key=step, author_name="Bertus")
        return LegalStepCommentAttachment.objects.create(comment=c, file=SimpleUploadedFile(name, data), original_name=name)

    def _send(self, **overrides):
        self.client.force_login(self.boss)
        page = self.client.get(reverse("xero_legal_lod_dispatch", args=[self.matter.id]))
        v = page.context["values"]
        data = {"inspector": v["inspector"], "to_email": "s.dlisani@afsq.co.za", "save_email": "1",
                "copy_me": "1", "cc": "", "due_on": v["due_on"], "subject": v["subject"], "body": v["body"],
                "docs": [str(d) for d in v["docs"]], "invoices": list(v["invoices"]), "statement": "1"}
        data.update(overrides)
        return self.client.post(reverse("xero_legal_lod_dispatch", args=[self.matter.id]), data)

    # ---- what is offered ----
    def test_the_inspector_comes_from_the_debtors_invoices(self):
        self.assertEqual(lod_dispatch.clean_inspector("020 - S Dlisani"), "S Dlisani")
        self.assertEqual(lod_dispatch.inspector_names("020 - S Dlisani, 050 - L Maqina"), ["S Dlisani", "L Maqina"])
        self.client.force_login(self.boss)
        page = self.client.get(reverse("xero_legal_lod_dispatch", args=[self.matter.id]))
        self.assertEqual(page.context["suggested"], ["S Dlisani", "L Maqina"])
        self.assertEqual(Inspector.objects.get(id=page.context["values"]["inspector"]).name, "S Dlisani")
        self.assertIn("Dear S Dlisani,", page.context["values"]["body"])
        self.assertContains(page, "No address saved yet")

    def test_only_the_letter_of_demand_and_cover_letter_are_ticked(self):
        picked = {d["name"] for d in lod_dispatch.matter_documents(self.matter) if d["picked"]}
        self.assertEqual(picked, {"Letter of Demand_Cover.pdf", "Letter of Demand_Annexures (1-7).pdf"})

    def test_the_statement_of_account_is_a_pdf(self):
        pdf = build_statement_pdf("Garden Route Abattoir", ["1 Main Road", "George"],
                                  OpenInvoiceSnapshot.objects.filter(contact_id="c-legal"))
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.client.force_login(self.boss)
        r = self.client.get(reverse("xero_legal_lod_statement", args=[self.matter.id]))
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/pdf"))

    # ---- sending ----
    def test_sending_emails_the_inspector_with_everything_attached(self):
        r = self._send()
        self.assertRedirects(r, reverse("xero_legal_matter", args=[self.matter.id]) + "#step-lod_hand_delivery",
                             fetch_redirect_response=False)
        kw = self.mail.call_args.kwargs
        self.assertEqual(kw["to"], ["s.dlisani@afsq.co.za"])
        self.assertEqual((kw["cc"], kw["reply_to"]), (["boss@fsa.test"], ["boss@fsa.test"]))
        names = [a[0] for a in kw["attachments"]]
        self.assertEqual(names, ["Letter of Demand_Cover.pdf", "Letter of Demand_Annexures (1-7).pdf",
                                 "Invoice l1.pdf", "Invoice l2.pdf", "Statement of account - Garden Route Abattoir.pdf"])
        ins = LodInstruction.objects.get()
        self.assertIn(ins.upload_url, kw["html_body"])
        self.assertTrue(ins.upload_url.endswith(reverse("lod_proof_upload", args=[ins.token])))
        self.assertEqual((ins.status, ins.inspector_name, ins.sent_by), ("sent", "S Dlisani", self.boss))
        self.assertEqual(ins.due_on, date.today() + timedelta(days=7))
        self.assertEqual(Inspector.objects.get(name="S Dlisani").email, "s.dlisani@afsq.co.za")   # saved
        audit = LegalStepComment.objects.filter(matter=self.matter, step_key="lod_hand_delivery").get()
        self.assertIn("Hand-delivery instruction emailed to S Dlisani", audit.text)
        self.assertIn("instructed S Dlisani to hand-deliver", DebtorNotice.objects.get(recipient=self.lawyer).text)

    def test_the_task_list_follows_the_instruction(self):
        self._send()
        m = LegalMatter.objects.prefetch_related("step_states", "payment_plans", "payments", "lod_instructions").get(id=self.matter.id)
        task = next(t for t in legal_board.board_state(m, timezone.now())["tasks"] if t["owner"] == "FSA")
        self.assertIn("Waiting for S Dlisani to deliver the LOD", task["text"])
        self.assertFalse(task["flag"])
        LodInstruction.objects.update(due_on=date.today() - timedelta(days=1))
        m = LegalMatter.objects.prefetch_related("step_states", "payment_plans", "payments", "lod_instructions").get(id=self.matter.id)
        task = next(t for t in legal_board.board_state(m, timezone.now())["tasks"] if t["owner"] == "FSA")
        self.assertIn("S Dlisani has not sent the signed proof", task["text"])
        self.assertTrue(task["flag"])

    def test_too_large_or_incomplete_is_not_sent(self):
        big = self._upload("draft_lod", "Letter of Demand big.pdf", b"x" * (lod_dispatch.MAX_ATTACHMENT_BYTES + 1))
        r = self._send(docs=[str(big.id)])
        self.assertContains(r, "email allows about")
        r = self._send(to_email="not-an-email")
        self.assertContains(r, "enter the inspector&#x27;s email address")
        self.assertFalse(LodInstruction.objects.exists())
        self.assertFalse(self.mail.called)

    def test_a_failed_email_records_nothing(self):
        self.mail.return_value = 0
        self.assertContains(self._send(), "could not be sent")
        self.assertFalse(LodInstruction.objects.exists())

    def test_attorneys_and_clerks_cannot_send(self):
        for user in (self.lawyer, self.anna):
            self.client.force_login(user)
            r = self.client.get(reverse("xero_legal_lod_dispatch", args=[self.matter.id]))
            self.assertRedirects(r, reverse("xero_legal"), fetch_redirect_response=False)
        helper = User.objects.create_user("fsa@fsa.test", "x", role="administrator", legal_access=True)
        self.client.force_login(helper)
        self.assertEqual(self.client.get(reverse("xero_legal_lod_dispatch", args=[self.matter.id])).status_code, 200)

    # ---- the inspector's upload link ----
    def test_the_inspector_uploads_the_signed_proof(self):
        self._send()
        ins = LodInstruction.objects.get()
        self.client.logout()
        url = reverse("lod_proof_upload", args=[ins.token])
        self.assertContains(self.client.get(url), "Garden Route Abattoir")
        self.assertContains(self.client.post(url, {}), "Choose the photo or scan")
        r = self.client.post(url, {"proof": SimpleUploadedFile("signed.jpg", b"\xff\xd8 photo"), "note": "Signed by Mr Smith"})
        self.assertContains(r, "Thank you")
        ins.refresh_from_db()
        self.assertEqual(ins.status, "delivered")
        step = LegalStep.objects.get(matter=self.matter, step_key="lod_hand_delivery")
        self.assertEqual((step.done, step.done_by), (True, "S Dlisani (inspector)"))
        proof = LegalStepCommentAttachment.objects.get(nature="Signed proof of delivery")
        self.assertEqual(proof.original_name, "signed.jpg")
        self.assertTrue(DebtorNotice.objects.filter(recipient=self.boss, kind=DebtorNotice.KIND_LOD_DELIVERED).exists())
        self.assertContains(self.client.get(url), "Already received")
        self.assertEqual(self.client.get(reverse("lod_proof_upload", args=["nope"])).status_code, 404)

    def test_fsa_uploading_the_proof_closes_the_instruction(self):
        self._send()
        self.client.post(reverse("xero_legal_step_comment", args=[self.matter.id]), {
            "step_key": "lod_hand_delivery", "confirm_delivery": "1",
            "documents": SimpleUploadedFile("proof.pdf", b"%PDF proof")})
        self.assertEqual(LodInstruction.objects.get().status, "delivered")

    def test_an_unsuccessful_delivery_is_recorded_on_the_matter(self):
        self._send()
        ins = LodInstruction.objects.get()
        self.client.post(reverse("xero_legal_lod_instruction", args=[ins.id]),
                         {"action": "unsuccessful", "reason": "closed", "note": "Premises empty"})
        ins.refresh_from_db()
        m = LegalMatter.objects.get(id=self.matter.id)
        self.assertEqual((ins.status, m.delivery_failed, m.delivery_failed_reason), ("unsuccessful", True, "closed"))
        self.assertContains(self.client.get(reverse("lod_proof_upload", args=[ins.token])), "no longer open")

    # ---- reminders ----
    def test_reminders_go_out_once_before_and_after_the_deadline(self):
        self._send()
        ins = LodInstruction.objects.get()
        self.mail.reset_mock()
        LodInstruction.objects.update(due_on=date.today() + timedelta(days=2))
        notifications.send_legal_diary_reminders(digest_hour=25)
        self.assertEqual(self.mail.call_count, 1)
        self.assertTrue(self.mail.call_args.kwargs["subject"].startswith("Reminder: "))
        self.assertIn(ins.upload_url, self.mail.call_args.kwargs["html_body"])
        notifications.send_legal_diary_reminders(digest_hour=25)
        self.assertEqual(self.mail.call_count, 1)                         # once only
        LodInstruction.objects.update(due_on=date.today() - timedelta(days=1))
        notifications.send_legal_diary_reminders(digest_hour=25)
        self.assertTrue(self.mail.call_args.kwargs["subject"].startswith("Overdue: "))
        notice = DebtorNotice.objects.filter(recipient=self.boss, kind=DebtorNotice.KIND_LEGAL_REMINDER).get()
        self.assertIn("Hand delivery overdue", notice.text)

    # ---- the lists ----
    def test_the_deliveries_page_and_address_book(self):
        self.client.force_login(self.boss)
        r = self.client.get(reverse("xero_legal_deliveries"))
        self.assertEqual([m.id for m in r.context["waiting"]], [self.matter.id])   # due, not yet sent
        self._send()
        r = self.client.get(reverse("xero_legal_deliveries"))
        self.assertEqual((r.context["counts"]["open"], r.context["waiting"]), (1, []))
        r = self.client.get(reverse("xero_inspectors"))
        self.assertEqual({i.name for i in r.context["inspectors"]}, {"S Dlisani", "L Maqina"})
        maqina = Inspector.objects.get(name="L Maqina")
        self.client.post(reverse("xero_inspectors"), {f"email_{maqina.id}": "l.maqina@afsq.co.za",
                                                      f"phone_{maqina.id}": "082", f"active_{maqina.id}": "1"})
        self.assertEqual(Inspector.objects.get(id=maqina.id).email, "l.maqina@afsq.co.za")
        self.client.post(reverse("xero_inspectors"), {f"email_{maqina.id}": "nonsense", f"active_{maqina.id}": "1"})
        self.assertEqual(Inspector.objects.get(id=maqina.id).email, "l.maqina@afsq.co.za")
