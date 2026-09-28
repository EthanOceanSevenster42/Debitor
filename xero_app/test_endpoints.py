"""Every endpoint in the app, for every role.

A page may answer 200, redirect, refuse (403) or not find something (404) - but
it must never fail with a server error, and nothing may reach Xero, WhatsApp
(WATI) or Microsoft Graph while the tests run: those calls are replaced with
stand-ins, and a guard fails the test if any real HTTP request slips through.
"""
import shutil
import tempfile
from unittest import mock

import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog
from . import legal_workflow
from .models import (CallLog, ClosedDebtor, DebtorAllocation, DebtorCategory,
                     DebtorClassification, DebtorComment, DebtorNotice,
                     HandoverInvoice, HandoverSetting, InvoiceComment, LegalMatter,
                     LegalStep, MessageTemplate, ReportRecipient, WriteOffInvoice)
from .tests import TENANT, BookFixture, User

_MEDIA = tempfile.mkdtemp(prefix="fsa-test-media-")


@override_settings(MEDIA_ROOT=_MEDIA)
class EndpointTests(BookFixture):

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.lawyer = User.objects.create_user("law@fsa.test", "x", role="lawyer")
        self.norole = User.objects.create_user("nobody@fsa.test", "x")
        self.comment = DebtorComment.objects.create(
            tenant_id=TENANT, contact_id="c-active", contact_name="Karoo Butchery",
            author=self.anna, author_name="Anna Clerk", text="Called, promised Friday.")
        self.notice = DebtorNotice.objects.create(
            tenant_id=TENANT, recipient=self.boss, actor_name="Anna Clerk",
            contact_id="c-active", contact_name="Karoo Butchery", comment=self.comment,
            text="Called, promised Friday.")
        self.template = MessageTemplate.objects.create(
            channel=MessageTemplate.CHANNEL_WHATSAPP, name="Reminder", body="Hi {name}",
            wati_template_name="reminder", is_default=True)

        # Nothing may leave the machine. Each outside service gets a harmless
        # stand-in, and any HTTP request that is not stubbed fails the test.
        self.network_calls = []

        def no_network(session, method, url, *a, **k):
            self.network_calls.append(f"{method} {url}")
            raise AssertionError(f"real network call attempted: {method} {url}")

        stubs = [
            mock.patch.object(requests.sessions.Session, "request", no_network),
            mock.patch("xero_app.views.fetch_invoice_history", return_value=[]),
            mock.patch("xero_app.views.fetch_contact", return_value=None),
            mock.patch("xero_app.views.fetch_online_invoice_url", return_value=""),
            mock.patch("xero_app.views.call_command", return_value=None),
            mock.patch("xero_app.views._xero_api_get", return_value={}),
            mock.patch("xero_app.wati.is_configured", return_value=False),
            mock.patch("xero_app.wati.list_templates", return_value=[]),
            mock.patch("xero_app.wati.template_status", return_value=None),
            mock.patch("xero_app.wati.send_template_message", return_value={"ok": False, "error": "test"}),
            mock.patch("xero_app.views.notifications.notify_new_matter_approved", return_value=0),
            mock.patch("xero_app.views.reports.send_lawyer_report", return_value=(0, {})),
            mock.patch("accounts.views.send_app_email", return_value=True),
        ]
        for s in stubs:
            s.start()
            self.addCleanup(s.stop)

    def tearDown(self):
        self.assertEqual(self.network_calls, [], "a test tried to reach the internet")
        super().tearDown()

    # ---- every page, every role ----------------------------------------------------

    def _pages(self):
        m = self.matter.id
        a = self.anna.id
        return [
            "/", "/audit/", "/users/", "/users/add/", "/users/invite/",
            f"/users/{a}/edit/", f"/users/{a}/reset-password/",
            "/xero/login/", "/xero/callback/", "/xero/callback/?error=access_denied",
            "/xero/dashboard/", f"/xero/dashboard/?admin={a}",
            "/xero/reports/", "/xero/reports/?period=week", "/xero/reports/?period=quarter",
            "/xero/reports/?period=year", "/xero/reports/?period=custom&from=2026-01-01&to=2026-12-31",
            "/xero/reports/?period=custom&from=bad&to=worse", "/xero/reports/?period=nonsense",
            f"/xero/reports/?clerk={a}", "/xero/reports/?clerk=unallocated", "/xero/reports/?clerk=99999",
            "/xero/reports/?category=none", "/xero/reports/?category=99999", "/xero/reports/?reason=no_contact",
            "/xero/reports/?export=xlsx", "/xero/reports/?period=year&export=xlsx",
            "/xero/categories/", "/xero/categories/?show=none", "/xero/categories/?show=manual&q=karoo",
            "/xero/aging/", "/xero/aging/?category=none", "/xero/aging/?q=garden", "/xero/aging/?stage=missed",
            "/xero/aging/?stage=call", "/xero/aging/?bucket=0-30", "/xero/aging/?admin=unallocated",
            f"/xero/aging/?admin={a}", "/xero/aging/?project=060",
            "/xero/closed/", "/xero/write-offs/", "/xero/handover/", "/xero/handover/overrides/",
            "/xero/legal/", f"/xero/legal/{m}/", f"/xero/legal/{m}/timeline/", "/xero/legal/99999/",
            "/xero/filing/", "/xero/filing/company/?cid=c-legal", "/xero/filing/company/",
            "/xero/debtor/statement/?cid=c-active&scope=open", "/xero/debtor/statement/?cid=c-ho&scope=handover",
            "/xero/debtor/statement/?cid=c-wo&scope=writeoff", "/xero/debtor/statement/?cid=c-closed&scope=closed",
            "/xero/debtor/statement/",
            "/xero/notifications/", "/xero/notices/",
            "/xero/invoice/a1/history/", "/xero/invoice/a1/report/", "/xero/invoice/a1/online/",
            "/xero/invoice/nope/report/",
            "/xero/company-report/?cid=c-active", "/xero/company-report/?cid=c-legal", "/xero/company-report/",
            "/xero/manual/", "/xero/schedule/", "/xero/lawyer-report/", "/xero/lawyer-report/preview/",
            "/xero/communication-setup/", "/xero/whatsapp-template/", "/xero/email-template/",
            "/xero/contact/c-active/", "/xero/aging/refresh/", "/xero/export/",
        ]

    def _visit_all(self):
        statuses = {}
        for url in self._pages():
            r = self.client.get(url)          # a server error raises here
            self.assertLess(r.status_code, 500, url)
            statuses[url] = r.status_code
        return statuses

    def test_every_page_as_a_super_admin(self):
        self.client.force_login(self.boss)
        statuses = self._visit_all()
        refused = {u: s for u, s in statuses.items() if s == 403}
        self.assertEqual(refused, {}, "a Super Admin was refused a page")
        for url in ("/xero/reports/", "/xero/categories/", "/xero/aging/", "/xero/dashboard/",
                    "/xero/legal/", "/xero/handover/", "/xero/manual/", "/users/", "/audit/"):
            self.assertEqual(statuses[url], 200, url)

    def test_every_page_as_a_clerk(self):
        self.client.force_login(self.anna)
        statuses = self._visit_all()
        for url in ("/xero/reports/", "/xero/aging/", "/xero/dashboard/", "/xero/handover/", "/xero/legal/"):
            self.assertEqual(statuses[url], 200, url)
        for url in ("/xero/categories/", "/users/", "/audit/", "/xero/manual/", "/xero/schedule/"):
            self.assertEqual(statuses[url], 403, url)

    def test_every_page_as_a_lawyer(self):
        self.client.force_login(self.lawyer)
        statuses = self._visit_all()
        self.assertEqual(statuses["/xero/legal/"], 200)
        for url in ("/xero/reports/", "/xero/aging/", "/xero/dashboard/", "/xero/handover/"):
            self.assertEqual(statuses[url], 302, url)

    def test_every_page_with_no_role_and_signed_out(self):
        self.client.force_login(self.norole)
        self._visit_all()
        self.client.logout()
        # The two retired template URLs are plain redirects to Communication
        # Setup, which then asks for a login itself.
        retired = {"/xero/whatsapp-template/", "/xero/email-template/"}
        for url in self._pages():
            if url in retired:
                continue
            r = self.client.get(url)
            self.assertIn(r.status_code, (301, 302), url)
            self.assertIn("/login/", r["Location"], url)

    def test_public_pages(self):
        for url in ("/login/", "/password-reset/", "/password-reset/sent/", "/reset/done/",
                    "/reset/bad/bad-token/", "/invite/bad/bad-token/"):
            r = self.client.get(url)
            self.assertLess(r.status_code, 500, url)

    # ---- what a clerk does all day ------------------------------------------------------

    def test_collections_actions_as_a_clerk(self):
        c = self.client
        c.force_login(self.anna)
        ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        c.post(reverse("xero_log_call"), {"invoice_id": "a1", "invoice_number": "a1", "contact_id": "c-active",
                                          "contact_name": "Karoo Butchery", "note": "Spoke to accounts"}, **ajax)
        c.post(reverse("xero_log_email", args=["a1"]), {"to": "accounts@karoo.test"}, **ajax)
        self.assertEqual(CallLog.objects.filter(invoice_id="a1").count(), 2)
        c.post(reverse("xero_unlog_contact", args=["a1"]), {"action_type": "email"}, **ajax)
        self.assertEqual(CallLog.objects.filter(invoice_id="a1").count(), 1)
        c.post(reverse("xero_add_comment", args=["a1"]),
               {"text": "Proof of payment", "comment_at": "2026-09-20T10:00", "nature": "Proof of payment",
                "documents": SimpleUploadedFile("pop.pdf", b"%PDF-1.4 test", content_type="application/pdf")})
        self.assertTrue(InvoiceComment.objects.filter(invoice_id="a1", text="Proof of payment").exists())
        r = c.post(reverse("xero_debtor_comment_add"), {"contact_id": "c-active", "contact_name": "Karoo Butchery",
                                                        "text": "Second note", "parent_id": self.comment.id}, **ajax)
        self.assertLess(r.status_code, 500)
        reply = DebtorComment.objects.get(text="Second note")
        c.post(reverse("xero_debtor_comment_delete"), {"id": reply.id}, **ajax)
        self.assertIsNotNone(DebtorComment.objects.get(id=reply.id).deleted_at)
        c.post(reverse("xero_followup_shift"), {"contact_id": "c-active", "contact_name": "Karoo Butchery",
                                                "shift_days": "14", "note": "Payment plan"})
        self.assertEqual(HandoverSetting.objects.get(contact_id="c-active").cadence_shift_days, 14)
        r = c.post(reverse("xero_send_whatsapp", args=["a1"]), {"template_id": self.template.id, "to": "27821234567"}, **ajax)
        self.assertLess(r.status_code, 500)          # WhatsApp is not set up in tests: refused, not crashed
        self.assertFalse(CallLog.objects.filter(action_type="whatsapp").exists())
        c.post(reverse("xero_handover_mark"), {"invoice_id": "a2", "invoice_number": "a2", "contact_id": "c-active",
                                               "contact_name": "Karoo Butchery", "reason": "Not paying"}, **ajax)
        self.assertTrue(HandoverInvoice.objects.filter(invoice_id="a2").exists())
        c.post(reverse("xero_write_off_invoice"), {"invoice_id": "a1", "contact_id": "c-active", "reason": "Dispute"}, **ajax)
        self.assertTrue(WriteOffInvoice.objects.filter(invoice_id="a1").exists())
        c.post(reverse("xero_notice_seen"), {"all": "1"})
        c.post(reverse("xero_notice_delete"), {"all": "1"})

    def test_clerks_cannot_take_management_decisions(self):
        c = self.client
        c.force_login(self.anna)
        store = DebtorCategory.objects.get(name="Individual stores")
        c.post(reverse("xero_close_debtor"), {"contact_id": "c-active"})
        c.post(reverse("xero_allocate_debtor"), {"contact_id": "c-closed", "administrator": self.anna.id})
        c.post(reverse("xero_debtor_category"), {"contact_id": "c-active", "category": store.id})
        c.post(reverse("xero_categories"), {"action": "add", "name": "Sneaky"})
        c.post(reverse("xero_legal_approve"), {"matter_id": self.matter.id})
        c.post(reverse("xero_legal_return"), {"matter_id": self.matter.id})
        c.post(reverse("xero_handover_unmark"), {"invoice_id": "h1"})
        c.post(reverse("user_delete", args=[self.ben.id]))
        self.assertFalse(ClosedDebtor.objects.filter(contact_id="c-active").exists())
        self.assertFalse(DebtorAllocation.objects.filter(contact_id="c-closed").exists())
        self.assertFalse(DebtorClassification.objects.exists())
        self.assertFalse(DebtorCategory.objects.filter(name="Sneaky").exists())
        self.assertEqual(LegalMatter.objects.get(id=self.matter.id).status, LegalMatter.ACTIVE)
        self.assertTrue(User.objects.filter(id=self.ben.id).exists())

    # ---- what a Super Admin does ---------------------------------------------------------

    def test_management_actions_as_a_super_admin(self):
        c = self.client
        c.force_login(self.boss)
        ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        c.post(reverse("xero_allocate_debtor"), {"contact_id": "c-closed", "contact_name": "Closed Co",
                                                 "administrator": self.ben.id}, **ajax)
        self.assertEqual(DebtorAllocation.objects.get(contact_id="c-closed").administrator, self.ben)
        c.post(reverse("xero_close_debtor"), {"contact_id": "c-active", "contact_name": "Karoo Butchery"})
        c.post(reverse("xero_reopen_debtor"), {"contact_id": "c-active"})
        self.assertFalse(ClosedDebtor.objects.filter(contact_id="c-active").exists())
        c.post(reverse("xero_write_off_debtor"), {"contact_id": "c-ret", "contact_name": "Riverside", "reason": "Gone"})
        self.assertTrue(WriteOffInvoice.objects.filter(invoice_id="r1").exists())
        c.post(reverse("xero_credit_note_toggle"), {"invoice_id": "r1"}, **ajax)
        self.assertTrue(WriteOffInvoice.objects.get(invoice_id="r1").credit_note_issued)
        c.post(reverse("xero_unwrite_off_invoice"), {"invoice_id": "r1", "note": "Paid after all"})
        self.assertFalse(WriteOffInvoice.objects.filter(invoice_id="r1").exists())
        c.post(reverse("xero_handover_debtor"), {"contact_id": "c-active", "contact_name": "Karoo", "reason": "Ignoring us"})
        c.post(reverse("xero_handover_unmark"), {"invoice_id": "a1", "note": "Back"})
        c.post(reverse("xero_handover_undo_debtor"), {"contact_id": "c-active", "contact_name": "Karoo"})
        for mode in ("days", "never", "default"):
            r = c.post(reverse("xero_handover_settings"), {"contact_id": "c-ret", "contact_name": "Riverside",
                                                           "mode": mode, "handover_days": "90"})
            self.assertLess(r.status_code, 500, mode)
        c.post(reverse("xero_legal_send"), {"contact_id": "c-ho", "contact_name": "Hilltop"})
        pending = LegalMatter.objects.get(contact_id="c-ho")
        c.post(reverse("xero_legal_approve"), {"matter_id": pending.id})
        self.assertEqual(LegalMatter.objects.get(id=pending.id).status, LegalMatter.ACTIVE)
        c.post(reverse("xero_legal_toggle_opposed", args=[pending.id]), {"which": "summons"})
        step = sorted(legal_workflow.ALL_STEP_KEYS)[0]
        c.post(reverse("xero_legal_step_toggle", args=[pending.id]), {"step_key": step})
        self.assertTrue(LegalStep.objects.filter(matter=pending, done=True).exists())
        c.post(reverse("xero_legal_step_comment", args=[pending.id]),
               {"step_key": step, "text": "Summons issued", "nature": "Summons",
                "documents": SimpleUploadedFile("summons.pdf", b"%PDF-1.4", content_type="application/pdf")})
        c.post(reverse("xero_legal_return"), {"matter_id": pending.id, "note": "Settled"})
        c.post(reverse("xero_legal_cancel"), {"matter_id": self.matter.id})
        self.assertEqual(LegalMatter.objects.get(id=self.matter.id).status, LegalMatter.CLOSED)

        cat = DebtorCategory.objects.get(name="Training")
        c.post(reverse("xero_categories"), {"action": "add", "name": "Government", "keywords": "municipality"})
        gov = DebtorCategory.objects.get(name="Government")
        c.post(reverse("xero_categories"), {"action": "update", "id": gov.id, "name": "Government",
                                            "keywords": "municipality, department", "sort_order": "5", "is_active": "on"})
        c.post(reverse("xero_categories"), {"action": "classify", "cid": ["c-active"], "category": cat.id})
        c.post(reverse("xero_categories"), {"action": "classify", "cid": ["c-active"], "category": "auto"})
        c.post(reverse("xero_categories"), {"action": "delete", "id": gov.id})
        self.assertFalse(DebtorCategory.objects.filter(name="Government").exists())
        r = c.post(reverse("xero_debtor_category"), {"contact_id": "c-ret", "category": cat.id}, **ajax)
        self.assertEqual(r.json()["source"], "manual")

        c.post(reverse("xero_schedule"), {"form": "golive", "go_live_date": "2026-08-01"})
        c.post(reverse("xero_schedule"), {"enabled": "on", "mode": "interval", "interval_hours": "2"})
        c.post(reverse("xero_lawyer_report"), {"action": "add_recipient", "email": "counsel@fsa.test", "name": "Counsel"})
        rec = ReportRecipient.objects.get(email="counsel@fsa.test")
        c.post(reverse("xero_lawyer_report"), {"action": "toggle_recipient", "recipient_id": rec.id})
        c.post(reverse("xero_lawyer_report"), {"action": "save_schedule", "enabled": "on", "frequency": "weekly",
                                               "day_of_week": "0", "send_time": "07:00"})
        c.post(reverse("xero_lawyer_report"), {"action": "send_now"})
        c.post(reverse("xero_lawyer_report"), {"action": "remove_recipient", "recipient_id": rec.id})
        c.post(reverse("xero_communication_setup"), {"action": "add", "channel": "email", "name": "Final notice",
                                                     "subject": "Final notice {invoice_number}", "body": "Dear {name}"})
        email_tpl = MessageTemplate.objects.get(name="Final notice")
        c.post(reverse("xero_communication_setup"), {"action": "make_default", "id": email_tpl.id})
        c.post(reverse("xero_communication_setup"), {"action": "delete", "id": email_tpl.id})

        c.post(reverse("user_create"), {"first_name": "New", "last_name": "Clerk", "email": "new@fsa.test",
                                        "role": "administrator", "password1": "Str0ng-pass-123!",
                                        "password2": "Str0ng-pass-123!"})
        new = User.objects.get(email="new@fsa.test")
        c.post(reverse("user_edit", args=[new.id]), {"first_name": "New", "last_name": "Clerk2",
                                                     "email": "new@fsa.test", "role": "administrator", "is_active": "on"})
        c.post(reverse("user_reset_password", args=[new.id]), {"new_password1": "An0ther-pass-456!",
                                                               "new_password2": "An0ther-pass-456!"})
        c.post(reverse("user_toggle_active", args=[new.id]))
        c.post(reverse("user_invite"), {"first_name": "Invited", "last_name": "Person",
                                        "email": "invited@fsa.test", "role": "lawyer"})
        invited = User.objects.get(email="invited@fsa.test")
        c.post(reverse("user_resend_invite", args=[invited.id]))
        c.post(reverse("user_delete", args=[new.id]))
        self.assertFalse(User.objects.filter(email="new@fsa.test").exists())
        # Every one of those changes was recorded in the audit log.
        self.assertGreater(AuditLog.objects.filter(action=AuditLog.ACTION_CHANGE).count(), 30)

        r = c.post(reverse("logout"))
        self.assertLess(r.status_code, 500)

    def test_the_xero_export_runs_without_calling_xero_for_real(self):
        self.client.force_login(self.boss)
        s = self.client.session
        s["xero_access_token"] = "test-token"
        s["xero_tenant_id"] = TENANT
        s.save()
        r = self.client.get("/xero/export/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])

    def test_a_lawyer_works_their_matter(self):
        c = self.client
        c.force_login(self.lawyer)
        step = sorted(legal_workflow.ALL_STEP_KEYS)[0]
        c.post(reverse("xero_legal_step_toggle", args=[self.matter.id]), {"step_key": step})
        c.post(reverse("xero_legal_step_comment", args=[self.matter.id]), {"step_key": step, "text": "Letter sent"})
        self.assertTrue(LegalStep.objects.filter(matter=self.matter, step_key=step, done=True).exists())
        # ...but cannot approve, close or bring back matters.
        c.post(reverse("xero_legal_cancel"), {"matter_id": self.matter.id})
        self.assertEqual(LegalMatter.objects.get(id=self.matter.id).status, LegalMatter.ACTIVE)
