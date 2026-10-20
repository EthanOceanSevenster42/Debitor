"""The Lawyers page tabs: each matter in one stage, Action required across them,
and promises / arrangements measured against the payments that come in."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from . import legal_board
from . import notifications
from .models import (DebtorNotice, LegalMatter, LegalPayment, LegalPaymentPlan, LegalReminder,
                     LegalStep, OpenInvoiceSnapshot, RecoveredInvoice)
from .tests import TENANT, BookFixture, User, _snap


def _days_ago(n):
    return timezone.now() - timedelta(days=n)


class _LegalBase(BookFixture):

    def setUp(self):
        super().setUp()
        self.lawyer = User.objects.create_user("law@fsa.test", "x", role="lawyer", first_name="Bertus")
        patcher = mock.patch("xero_app.notifications.send_app_email", return_value=1)
        self.email = patcher.start()
        self.addCleanup(patcher.stop)

    # ---- helpers ----
    def _tick(self, key, days_ago=1):
        LegalStep.objects.update_or_create(matter=self.matter, step_key=key, defaults={
            "done": True, "done_at": _days_ago(days_ago), "done_by": "Bertus"})

    def _paid(self, amount, days_ago=0):
        r = RecoveredInvoice.objects.create(tenant_id=TENANT, invoice_id="l1", contact_id="c-legal",
                                            contact_name="Garden Route Abattoir", amount=Decimal(amount),
                                            credited=True, reason=RecoveredInvoice.REASON_COLLECTED_LEGAL)
        RecoveredInvoice.objects.filter(id=r.id).update(recovered_at=_days_ago(days_ago))

    def _plan(self, kind, amount, first_date, recorded_days_ago=0, **kw):
        p = LegalPaymentPlan.objects.create(matter=self.matter, kind=kind, amount=Decimal(amount),
                                            first_date=first_date, **kw)
        LegalPaymentPlan.objects.filter(id=p.id).update(created_at=_days_ago(recorded_days_ago))
        return p

    def _state(self, days_idle=0):
        m = LegalMatter.objects.prefetch_related("step_states", "payment_plans").get(id=self.matter.id)
        pays = legal_board.payments_by_contact(TENANT, [m])[m.id]
        return legal_board.board_state(m, timezone.now(), pays, days_idle)

    def _page(self, user=None, tab=""):
        self.client.force_login(user or self.lawyer)
        return self.client.get(reverse("xero_legal") + (f"?tab={tab}" if tab else ""))


class LegalBoardTests(_LegalBase):

    # ---- one stage per matter ----
    def test_a_matter_moves_through_the_stages(self):
        self.assertEqual(self._state()["stage"], legal_board.NEW)
        self._tick("lod_email", 3)
        self.assertEqual(self._state()["stage"], legal_board.LOD_ISSUED)
        LegalMatter.objects.filter(id=self.matter.id).update(delivery_failed=True, delivery_failed_reason="no_trace")
        self.assertEqual(self._state()["stage"], legal_board.NO_TRACE)
        self._tick(legal_board.legal_workflow.LOD_HAND_DELIVERY, 1)
        self.assertEqual(self._state()["stage"], legal_board.HAND_DELIVERED)
        self._tick("su_summons_issued", 0)
        state = self._state()
        self.assertEqual(state["stage"], legal_board.LITIGATION)
        self.assertIn("Summons Issued", state["detail"])
        self._plan(LegalPaymentPlan.PROMISE, "1000", date.today() + timedelta(days=5))
        self.assertEqual(self._state()["stage"], legal_board.PROMISE)
        LegalMatter.objects.filter(id=self.matter.id).update(status=LegalMatter.CLOSED, closed_at=timezone.now())
        self.assertEqual(self._state()["stage"], legal_board.CLOSED)
        self.assertEqual(self._state()["actions"], [])           # nothing to do on a closed matter

    def test_the_stage_tabs_add_up_to_every_matter(self):
        LegalMatter.objects.create(tenant_id=TENANT, contact_id="c-ho", contact_name="Hilltop Store",
                                   status=LegalMatter.PENDING)
        r = self._page(self.boss, tab="all")
        tabs = {t["key"]: t for t in r.context["tabs"]}
        stage_keys = [k for k, _l, _h in legal_board.STAGES if k not in (legal_board.ACTION, legal_board.ALL)]
        self.assertEqual(sum(tabs[k]["count"] for k in stage_keys), tabs["all"]["count"])
        self.assertEqual(tabs["pending"]["count"], 1)
        self.assertEqual(tabs["new"]["total"], Decimal("5250"))
        # The attorneys do not get the approval tab or see the pending matter.
        r = self._page(self.lawyer, tab="all")
        self.assertNotIn("pending", [t["key"] for t in r.context["tabs"]])
        self.assertEqual([m.contact_name for m in r.context["rows"]], ["Garden Route Abattoir"])

    # ---- action required ----
    def test_action_required_says_what_and_who(self):
        LegalMatter.objects.filter(id=self.matter.id).update(approved_at=_days_ago(10))
        self.assertEqual([a["owner"] for a in self._state()["actions"]], ["Attorneys"])   # not started
        self._tick("lod_email", 9)
        owners = {a["owner"]: a["text"] for a in self._state()["actions"]}
        self.assertIn("Hand delivery outstanding since", owners["FSA"])
        self._tick("lod_email", 30)
        self.assertTrue(any("28 days since the LOD" in a["text"] for a in self._state()["actions"]))
        self._tick("default_listing_28", 1)
        self.assertFalse(any("28 days" in a["text"] for a in self._state()["actions"]))

    def test_the_page_opens_on_action_required_and_lists_it(self):
        self._tick("lod_email", 9)
        r = self._page()
        self.assertEqual(r.context["tab"], legal_board.ACTION)
        self.assertContains(r, "Hand delivery outstanding since")
        self.assertContains(r, "Garden Route Abattoir")
        # Nothing to do: it opens on All matters instead.
        LegalStep.objects.all().delete()
        self.assertEqual(self._page().context["tab"], legal_board.ALL)

    def test_a_follow_up_shows_once_it_is_due(self):
        LegalMatter.objects.filter(id=self.matter.id).update(
            follow_up_on=date.today() + timedelta(days=2), follow_up_note="Check the plea", follow_up_by="Bertus")
        self.assertEqual(self._state()["actions"], [])
        LegalMatter.objects.filter(id=self.matter.id).update(follow_up_on=date.today())
        action = self._state()["actions"][0]
        self.assertEqual((action["owner"], action["text"], action["due"]),
                         ("Bertus", "Follow-up: Check the plea", date.today()))

    # ---- promises and arrangements against real payments ----
    def test_a_promise_is_checked_against_payments(self):
        self._plan(LegalPaymentPlan.PROMISE, "3000", date.today() - timedelta(days=5), recorded_days_ago=10)
        self._paid("500", days_ago=20)          # before the promise: does not count
        self._paid("1000", days_ago=2)
        state = self._state()
        self.assertEqual(state["progress"]["received"], Decimal("1000"))
        self.assertTrue(state["progress"]["behind"])
        self.assertIn("Promise not kept", state["actions"][0]["text"])
        self._paid("2000", days_ago=1)
        state = self._state()
        self.assertTrue(state["progress"]["paid_in_full"])
        self.assertIn("paid in full", state["actions"][0]["text"])

    def test_a_promise_is_not_flagged_inside_the_grace_days(self):
        self._plan(LegalPaymentPlan.PROMISE, "3000", date.today() - timedelta(days=1))
        state = self._state(days_idle=30)
        self.assertFalse(state["progress"]["behind"])
        self.assertEqual(state["actions"], [])   # on track: no "no activity" alarm either

    def test_an_arrangement_tracks_instalments(self):
        self._plan(LegalPaymentPlan.ARRANGEMENT, "1000", date.today() - timedelta(days=40),
                   recorded_days_ago=45, frequency=LegalPaymentPlan.MONTHLY, instalments=6)
        self._paid("1000", days_ago=38)
        state = self._state()
        self.assertEqual(state["stage"], legal_board.ARRANGEMENT)
        pr = state["progress"]
        self.assertEqual((pr["due_count"], pr["paid_count"], pr["shortfall"]), (2, 1, Decimal("1000")))
        self.assertEqual(pr["total"], Decimal("6000"))
        self.assertIn("Instalment 2 missed", state["actions"][0]["text"])
        self.assertIn("arrangement behind by R 1,000.00", state["actions"][0]["text"])
        self.assertIn("1 of 6 paid", state["detail"])

    def test_monthly_instalments_keep_to_the_end_of_short_months(self):
        plan = LegalPaymentPlan(kind=LegalPaymentPlan.ARRANGEMENT, first_date=date(2026, 1, 31),
                                frequency=LegalPaymentPlan.MONTHLY, amount=Decimal(1))
        self.assertEqual([legal_board.instalment_date(plan, k) for k in range(3)],
                         [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)])

    # ---- recording it on the matter ----
    def test_the_attorney_records_a_promise_and_fsa_is_told(self):
        self.client.force_login(self.lawyer)
        url = reverse("xero_legal_plan", args=[self.matter.id])
        r = self.client.post(url, {"kind": "promise", "amount": "R 12,500.00",
                                   "first_date": "2026-11-15", "note": "Owner promised on the phone"})
        self.assertRedirects(r, reverse("xero_legal_matter", args=[self.matter.id]) + "#status",
                             fetch_redirect_response=False)
        plan = LegalPaymentPlan.objects.get(matter=self.matter)
        self.assertEqual((plan.amount, plan.status, plan.created_by), (Decimal("12500.00"), "open", "Bertus"))
        notice = DebtorNotice.objects.get(recipient=self.boss)
        self.assertIn("promise to pay R 12,500.00 by 15 Nov 2026", notice.text)
        # A new arrangement replaces it.
        self.client.post(url, {"kind": "arrangement", "amount": "2000", "frequency": "monthly",
                               "first_date": "2026-12-01", "instalments": "6"})
        self.assertEqual(sorted(LegalPaymentPlan.objects.values_list("kind", "status")),
                         [("arrangement", "open"), ("promise", "cancelled")])
        self.client.post(reverse("xero_legal_plan_close", args=[self.matter.id]), {"outcome": "kept"})
        self.assertFalse(LegalPaymentPlan.objects.filter(status="open").exists())

    def test_bad_plan_input_is_refused(self):
        self.client.force_login(self.lawyer)
        self.client.post(reverse("xero_legal_plan", args=[self.matter.id]),
                         {"kind": "arrangement", "amount": "-5", "first_date": "soon"})
        self.assertFalse(LegalPaymentPlan.objects.exists())

    def test_unsuccessful_delivery_until_it_is_delivered(self):
        self.client.force_login(self.lawyer)
        self._tick("lod_email", 9)
        self.client.post(reverse("xero_legal_delivery_failed", args=[self.matter.id]),
                         {"reason": "no_trace", "note": "Building empty"})
        m = LegalMatter.objects.get(id=self.matter.id)
        self.assertEqual((m.delivery_failed, m.delivery_failed_reason), (True, "no_trace"))
        state = self._state()
        self.assertEqual(state["stage"], legal_board.NO_TRACE)
        self.assertNotIn("FSA", [a["owner"] for a in state["actions"]])   # no hand-delivery nag
        self.assertContains(self._page(tab="no_trace"), "No trace - the facility could not be found - Building empty")
        self.client.post(reverse("xero_legal_delivery_failed", args=[self.matter.id]), {"clear": "1"})
        self.assertFalse(LegalMatter.objects.get(id=self.matter.id).delivery_failed)

    def test_follow_up_can_be_set_and_cleared(self):
        self.client.force_login(self.lawyer)
        url = reverse("xero_legal_follow_up", args=[self.matter.id])
        self.client.post(url, {"follow_up_on": date.today().isoformat(), "note": "Call the sheriff"})
        self.assertContains(self._page(tab="action"), "Call the sheriff")
        self.client.post(url, {"clear": "1"})
        self.assertIsNone(LegalMatter.objects.get(id=self.matter.id).follow_up_on)

    def test_a_clerk_without_lawyers_access_cannot_change_the_status(self):
        self.client.force_login(self.anna)
        self.client.post(reverse("xero_legal_plan", args=[self.matter.id]),
                         {"kind": "promise", "amount": "100", "first_date": "2026-11-01"})
        self.client.post(reverse("xero_legal_follow_up", args=[self.matter.id]), {"follow_up_on": "2026-11-01"})
        self.assertFalse(LegalPaymentPlan.objects.exists())
        self.assertIsNone(LegalMatter.objects.get(id=self.matter.id).follow_up_on)
        page = self.client.get(reverse("xero_legal_matter", args=[self.matter.id]))
        self.assertNotContains(page, "+ Promise to pay")

    def test_the_matter_page_shows_the_path_and_plan(self):
        self._tick("lod_email", 3)
        self._plan(LegalPaymentPlan.PROMISE, "750", date.today() + timedelta(days=4))
        self.client.force_login(self.lawyer)
        page = self.client.get(reverse("xero_legal_matter", args=[self.matter.id]))
        self.assertEqual([p["state"] for p in page.context["path"]],
                         ["done", "current", "todo", "todo", "todo"])
        self.assertContains(page, "Status &amp; payments")
        self.assertContains(page, "Mark kept")
        self.assertContains(page, "R&nbsp;750.00 by")


class LegalDiaryAndClosureTests(_LegalBase):
    """Points 2-6 of the request: the diary and reminders, instalments, payments
    and closure, IMI / APS, and who is responsible."""

    # ---- instalments ----
    def test_the_amount_to_settle_decides_the_instalments(self):
        self._plan(LegalPaymentPlan.ARRANGEMENT, "1000", date.today() + timedelta(days=1),
                   frequency=LegalPaymentPlan.MONTHLY, total_amount=Decimal("2500"))
        pr = self._state()["progress"]
        self.assertEqual(pr["count"], 3)
        self.assertEqual([r["amount"] for r in pr["schedule"]], [Decimal("1000"), Decimal("1000"), Decimal("500")])
        self.assertEqual((pr["balance"], pr["next_n"]), (Decimal("2500"), 1))

    def test_a_payment_in_xero_and_recorded_by_the_attorneys_counts_once(self):
        self._plan(LegalPaymentPlan.PROMISE, "1000", date.today() + timedelta(days=3), recorded_days_ago=5)
        self._paid("600", days_ago=1)
        LegalPayment.objects.create(matter=self.matter, amount=Decimal("600"), paid_on=date.today())
        self.assertEqual(self._state()["progress"]["received"], Decimal("600"))
        LegalPayment.objects.create(matter=self.matter, amount=Decimal("400"), paid_on=date.today())
        self.assertTrue(self._state()["progress"]["paid_in_full"])

    # ---- reminders ----
    def test_the_attorney_is_asked_to_check_a_promised_payment_on_the_day(self):
        LegalMatter.objects.filter(id=self.matter.id).update(attorney=self.lawyer)
        self._plan(LegalPaymentPlan.PROMISE, "800", date.today() + timedelta(days=1))
        self.assertEqual(notifications.send_legal_diary_reminders(digest_hour=25), 0)
        LegalPaymentPlan.objects.update(first_date=date.today())
        self.assertEqual(notifications.send_legal_diary_reminders(digest_hour=25), 1)
        notice = DebtorNotice.objects.get(kind=DebtorNotice.KIND_LEGAL_REMINDER)
        self.assertEqual(notice.recipient, self.lawyer)
        self.assertIn("promised payment of R 800.00", notice.text)
        self.assertEqual(notifications.send_legal_diary_reminders(digest_hour=25), 0)   # once only

    def test_a_missed_payment_is_flagged_to_the_attorney_and_fsa(self):
        self._plan(LegalPaymentPlan.PROMISE, "800", date.today() - timedelta(days=6), recorded_days_ago=10)
        self.assertEqual(notifications.send_legal_diary_reminders(digest_hour=25), 1)
        self.assertEqual(set(DebtorNotice.objects.filter(kind=DebtorNotice.KIND_LEGAL_REMINDER)
                             .values_list("recipient__email", flat=True)), {"law@fsa.test", "boss@fsa.test"})
        self.assertIn("Payment missed", DebtorNotice.objects.first().text)

    def test_a_follow_up_reminds_whoever_set_it(self):
        LegalMatter.objects.filter(id=self.matter.id).update(
            follow_up_on=date.today(), follow_up_note="Call the sheriff", follow_up_user=self.boss)
        notifications.send_legal_diary_reminders(digest_hour=25)
        notice = DebtorNotice.objects.get(kind=DebtorNotice.KIND_LEGAL_REMINDER)
        self.assertEqual((notice.recipient, notice.matter), (self.boss, self.matter))

    def test_everyone_gets_one_list_a_day_of_what_is_theirs(self):
        LegalMatter.objects.filter(id=self.matter.id).update(approved_at=_days_ago(10))
        self._tick("lod_email", 9)      # FSA: hand delivery overdue. Attorneys: nothing overdue yet.
        self.email.reset_mock()
        notifications.send_legal_diary_reminders(digest_hour=0)
        subjects = {c.kwargs["to"][0]: c.kwargs["subject"] for c in self.email.call_args_list}
        self.assertIn("Your legal diary", subjects["boss@fsa.test"])
        self.assertNotIn("law@fsa.test", subjects)
        self.email.reset_mock()
        notifications.send_legal_diary_reminders(digest_hour=0)
        self.assertFalse(any("diary" in c.kwargs["subject"] for c in self.email.call_args_list))
        self.assertTrue(LegalReminder.objects.filter(key__startswith="digest:").exists())

    # ---- payments and closure ----
    def test_the_attorney_records_a_payment_with_proof(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.force_login(self.lawyer)
        with self.settings(MEDIA_ROOT=__import__("tempfile").mkdtemp()):
            self.client.post(reverse("xero_legal_payment", args=[self.matter.id]), {
                "amount": "1500", "paid_on": date.today().isoformat(), "reference": "EFT 77",
                "proof": SimpleUploadedFile("pop.pdf", b"%PDF")})
        pay = LegalPayment.objects.get()
        self.assertEqual((pay.amount, pay.reference, pay.proof_name), (Decimal("1500"), "EFT 77", "pop.pdf"))
        self.assertEqual(self._state()["received"], Decimal("1500"))
        # Not a future date.
        self.client.post(reverse("xero_legal_payment", args=[self.matter.id]),
                         {"amount": "10", "paid_on": (date.today() + timedelta(days=2)).isoformat()})
        self.assertEqual(LegalPayment.objects.count(), 1)

    def test_an_attorney_recommends_closure_and_fsa_approves_it(self):
        self.client.force_login(self.lawyer)
        url = reverse("xero_legal_close", args=[self.matter.id])
        self.client.post(url, {"reason": "insolvent"})                     # no explanation: refused
        self.assertIsNone(LegalMatter.objects.get(id=self.matter.id).closure_requested_at)
        self.client.post(url, {"reason": "insolvent", "note": "Final liquidation order granted"})
        m = LegalMatter.objects.get(id=self.matter.id)
        self.assertEqual((m.status, m.closure_requested_reason), (LegalMatter.ACTIVE, "insolvent"))
        self.assertIn("your approval is needed", DebtorNotice.objects.get(recipient=self.boss).text)
        self.assertEqual(self._state()["actions"][0]["owner"], "FSA")
        # Only a Super Admin can decide.
        self.client.post(reverse("xero_legal_closure_decide", args=[self.matter.id]), {"decision": "approve"})
        self.assertEqual(LegalMatter.objects.get(id=self.matter.id).status, LegalMatter.ACTIVE)
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_closure_decide", args=[self.matter.id]), {"decision": "approve"})
        m = LegalMatter.objects.get(id=self.matter.id)
        self.assertEqual((m.status, m.closure_reason, m.closure_note),
                         (LegalMatter.CLOSED, "insolvent", "Final liquidation order granted"))
        self.assertIn("Debtor insolvent or bankrupt", self._state()["detail"])
        self.assertTrue(m.step_comments.filter(step_key="_status", text__startswith="Closed the matter").exists())

    def test_fsa_can_decline_a_recommended_closure(self):
        LegalMatter.objects.filter(id=self.matter.id).update(
            closure_requested_reason="untraceable", closure_requested_by="Bertus",
            closure_requested_at=timezone.now())
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_closure_decide", args=[self.matter.id]),
                         {"decision": "decline", "note": "Try the new address first"})
        m = LegalMatter.objects.get(id=self.matter.id)
        self.assertEqual((m.status, m.closure_requested_at), (LegalMatter.ACTIVE, None))

    def test_a_settlement_xero_confirms_closes_straight_away(self):
        self.client.force_login(self.lawyer)
        self.client.post(reverse("xero_legal_close", args=[self.matter.id]), {"reason": "settled"})
        self.assertEqual(LegalMatter.objects.get(id=self.matter.id).status, LegalMatter.ACTIVE)   # Xero: R 5,250 owing
        OpenInvoiceSnapshot.objects.filter(contact_id="c-legal").delete()
        LegalMatter.objects.filter(id=self.matter.id).update(closure_requested_at=None)
        self.assertIn("Nothing is owed in Xero", self._page(tab="action").content.decode())
        self.client.post(reverse("xero_legal_close", args=[self.matter.id]), {"reason": "settled"})
        m = LegalMatter.objects.get(id=self.matter.id)
        self.assertEqual((m.status, m.closure_reason), (LegalMatter.CLOSED, "settled"))

    # ---- IMI / APS and the responsible attorney ----
    def test_imi_and_aps_come_from_the_project_codes_and_can_be_set(self):
        LegalMatter.objects.create(tenant_id=TENANT, contact_id="c-ho", contact_name="Hilltop Store",
                                   status=LegalMatter.ACTIVE, approved_at=timezone.now())
        OpenInvoiceSnapshot.objects.filter(contact_id="c-legal").update(project_code="010 - Red Meat")
        OpenInvoiceSnapshot.objects.filter(contact_id="c-ho").update(
            project_code="060 - Processed Meat Products, 050 - Raw Meat Products")
        names = lambda r: sorted(m.contact_name for m in r.context["rows"])
        self.assertEqual(names(self._page(self.boss, tab="all&division=imi")), ["Garden Route Abattoir"])
        self.assertEqual(names(self._page(self.boss, tab="all&division=aps")), ["Hilltop Store"])
        self.client.post(reverse("xero_legal_assign", args=[self.matter.id]), {"division": "aps"})
        self.assertEqual(names(self._page(self.boss, tab="all&division=aps")),
                         ["Garden Route Abattoir", "Hilltop Store"])
        self.assertEqual(LegalMatter.objects.get(id=self.matter.id).division_set_by, "boss@fsa.test")

    def test_assigning_an_attorney_tells_them_and_filters_the_list(self):
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_legal_assign", args=[self.matter.id]), {"attorney": str(self.lawyer.id)})
        self.assertEqual(LegalMatter.objects.get(id=self.matter.id).attorney, self.lawyer)
        self.assertIn("made you the responsible attorney", DebtorNotice.objects.get(recipient=self.lawyer).text)
        self.assertEqual(len(self._page(self.lawyer, tab="all&attorney=mine").context["rows"]), 1)
        self.assertEqual(len(self._page(self.lawyer, tab="all&attorney=none").context["rows"]), 0)

    def test_the_first_attorney_to_work_a_matter_takes_it_on(self):
        self.client.force_login(self.lawyer)
        self.client.post(reverse("xero_legal_step_toggle", args=[self.matter.id]), {"step_key": "receipt_instruction"})
        self.assertEqual(LegalMatter.objects.get(id=self.matter.id).attorney, self.lawyer)

    def test_the_diary_lists_what_is_due_by_day(self):
        LegalMatter.objects.filter(id=self.matter.id).update(approved_at=_days_ago(10), attorney=self.lawyer)
        self._plan(LegalPaymentPlan.PROMISE, "900", date.today() + timedelta(days=3))
        self.client.force_login(self.lawyer)
        r = self.client.get(reverse("xero_legal_diary"))
        sections = {k: items for k, _label, items in r.context["sections"]}
        self.assertIn("promised payment of R 900.00", sections["week"][0]["text"])
        self.assertContains(r, "Garden Route Abattoir")
        # FSA's own view leaves out the attorneys' tasks.
        self.client.force_login(self.boss)
        r = self.client.get(reverse("xero_legal_diary") + "?who=mine")
        self.assertEqual(sum(len(i) for _k, _l, i in r.context["sections"]), 0)

    def test_attorneys_see_closed_matters_but_cannot_change_them(self):
        LegalMatter.objects.filter(id=self.matter.id).update(
            status=LegalMatter.CLOSED, closed_at=timezone.now(), closure_reason="settled")
        LegalMatter.objects.create(tenant_id=TENANT, contact_id="c-ho", contact_name="Hilltop Store",
                                   status=LegalMatter.CLOSED, closure_reason="withdrawn")   # never approved
        r = self._page(self.lawyer, tab="closed")
        self.assertEqual([m.contact_name for m in r.context["rows"]], ["Garden Route Abattoir"])
        self.client.force_login(self.lawyer)
        page = self.client.get(reverse("xero_legal_matter", args=[self.matter.id]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "+ Comment / document")
        self.client.post(reverse("xero_legal_step_toggle", args=[self.matter.id]), {"step_key": "draft_lod"})
        self.assertFalse(LegalStep.objects.filter(matter=self.matter, step_key="draft_lod").exists())

    def test_the_matter_page_shows_everything_at_a_glance(self):
        self._plan(LegalPaymentPlan.ARRANGEMENT, "1000", date.today() + timedelta(days=5),
                   frequency=LegalPaymentPlan.MONTHLY, instalments=3)
        self.client.force_login(self.boss)
        page = self.client.get(reverse("xero_legal_matter", args=[self.matter.id]))
        for text in ("Responsible attorney", "IMI / APS", "Next action", "Upcoming", "Payments since the handover",
                     "Documents (0)", "Close the matter", "Record a payment received"):
            self.assertContains(page, text)
