from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from . import performance, portfolio, recovery
from .models import (CallLog, ClosedDebtor, DebtorAllocation, DebtorCategory,
                     DebtorClassification, HandoverReturn, LegalMatter,
                     OpenInvoiceSnapshot, PortfolioSnapshot, RecoveredInvoice,
                     WriteOffInvoice, XeroConnection)
from .performance import Period, Scope, period_for
from .views import _bucket_for

User = get_user_model()
TENANT = "tenant-1"


# ---- pure logic (no database) ----------------------------------------------------

class PeriodTests(SimpleTestCase):
    def test_week_runs_monday_to_sunday(self):
        p = period_for("week", date(2026, 9, 16))          # a Wednesday
        self.assertEqual((p.start, p.end), (date(2026, 9, 14), date(2026, 9, 20)))
        self.assertEqual(p.previous().start, date(2026, 9, 7))
        self.assertEqual(p.next().start, date(2026, 9, 21))

    def test_month_quarter_year(self):
        m = period_for("month", date(2026, 2, 10))
        self.assertEqual((m.start, m.end), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(m.previous().start, date(2026, 1, 1))
        q = period_for("quarter", date(2026, 9, 28))
        self.assertEqual((q.start, q.end), (date(2026, 7, 1), date(2026, 9, 30)))
        self.assertEqual(q.previous().start, date(2026, 4, 1))
        self.assertEqual(period_for("quarter", date(2026, 12, 5)).end, date(2026, 12, 31))
        y = period_for("year", date(2026, 6, 1))
        self.assertEqual((y.start, y.end), (date(2026, 1, 1), date(2026, 12, 31)))

    def test_custom_previous_is_the_same_length_immediately_before(self):
        p = period_for("custom", date(2026, 9, 28), date(2026, 9, 11), date(2026, 9, 20))
        self.assertEqual(p.days, 10)
        prev = p.previous()
        self.assertEqual((prev.start, prev.end), (date(2026, 9, 1), date(2026, 9, 10)))

    def test_a_custom_range_of_whole_months_steps_by_months(self):
        # 2024 is a leap year: stepping by days would drift to "2 Jan 2024 - 1 Jan 2025".
        p = period_for("custom", date(2026, 9, 28), date(2026, 1, 1), date(2026, 12, 31))
        back = p.previous().previous()
        self.assertEqual((back.start, back.end), (date(2024, 1, 1), date(2024, 12, 31)))
        q = period_for("custom", date(2026, 9, 28), date(2026, 7, 1), date(2026, 9, 30))
        self.assertEqual((q.next().start, q.next().end), (date(2026, 10, 1), date(2026, 12, 31)))
        self.assertEqual(p.previous().short, "1 Jan - 31 Dec '25")

    def test_request_parsing_falls_back_to_this_month(self):
        today = date(2026, 9, 28)
        p = performance.period_from_request({"period": "nonsense"}, today=today)
        self.assertEqual((p.kind, p.start), ("month", date(2026, 9, 1)))
        p = performance.period_from_request({"period": "custom", "from": "2026-09-01"}, today=today)
        self.assertEqual((p.start, p.end), (date(2026, 9, 1), today))

    def test_trend_ends_on_the_selected_period(self):
        p = period_for("month", date(2026, 9, 1))
        trend = performance.trend_periods(p)
        self.assertEqual(len(trend), 12)
        self.assertEqual(trend[-1], p)
        self.assertEqual(trend[0].start, date(2025, 10, 1))


class ScopeTests(SimpleTestCase):
    def test_clerk_and_category_matching(self):
        self.assertTrue(Scope().clerk_ok(None) and Scope().category_ok(5))
        self.assertTrue(Scope(clerk="unallocated").clerk_ok(None))
        self.assertFalse(Scope(clerk="unallocated").clerk_ok(3))
        self.assertTrue(Scope(clerk=3).clerk_ok(3))
        self.assertFalse(Scope(clerk=3).clerk_ok(4))
        self.assertTrue(Scope(category="none").category_ok(None))
        self.assertFalse(Scope(category=2).category_ok(None))


class ChartTests(SimpleTestCase):
    def test_columns_round_the_data_end_only(self):
        chart = performance.column_chart([("A", "a", 100), ("B", "b", 0)])
        self.assertIn("Q", chart["cols"][0]["path"])        # rounded top
        self.assertEqual(chart["cols"][1]["path"], "")      # nothing drawn for zero
        self.assertTrue(chart["has_data"])

    def test_stacked_leaves_gaps_and_marks_missing_periods(self):
        chart = performance.stacked_chart([
            ("Aug", "Aug", {"0-30": 50, "31-60": 50}),
            ("Sep", "Sep", None),
        ])
        self.assertEqual(len(chart["cols"][0]["segs"]), 2)
        self.assertTrue(chart["cols"][1]["missing"])
        self.assertEqual(len(chart["legend"]), len(portfolio.BUCKETS))


# ---- fixtures ---------------------------------------------------------------------

def _snap(invoice_id, cid, name, dpd, amount, project=""):
    today = timezone.localdate()
    return OpenInvoiceSnapshot.objects.create(
        tenant_id=TENANT, invoice_id=invoice_id, invoice_number=invoice_id,
        contact_id=cid, contact_name=name, days_past_due=dpd, bucket=_bucket_for(dpd),
        amount_due=Decimal(amount), total=Decimal(amount), status="AUTHORISED",
        due_date=today - timedelta(days=dpd), invoice_date=today - timedelta(days=dpd + 30),
        project_code=project)


class BookFixture(TestCase):
    """A small book with one debtor in each situation the pages distinguish."""

    def setUp(self):
        self.boss = User.objects.create_user("boss@fsa.test", "x", role="super_admin")
        self.anna = User.objects.create_user("anna@fsa.test", "x", role="administrator",
                                             first_name="Anna", last_name="Clerk")
        self.ben = User.objects.create_user("ben@fsa.test", "x", role="administrator",
                                            first_name="Ben", last_name="Clerk")
        XeroConnection.objects.create(tenant_id=TENANT, tenant_name="FSA", access_token="a",
                                      refresh_token="", token_expires_at=timezone.now() + timedelta(hours=1))
        # Active collections.
        _snap("a1", "c-active", "Karoo Butchery", 20, "1000")
        # Aged past the handover threshold, plus a fresh invoice that goes with it.
        _snap("h1", "c-ho", "Hilltop Store", 70, "700")
        _snap("h2", "c-ho", "Hilltop Store", 5, "300")
        # Aged, but deliberately taken back out of handover.
        _snap("r1", "c-ret", "Riverside Spar", 80, "800")
        HandoverReturn.objects.create(tenant_id=TENANT, invoice_id="r1", contact_id="c-ret")
        # With the attorneys: an aged invoice and a fresh one.
        _snap("l1", "c-legal", "Garden Route Abattoir", 90, "5000")
        _snap("l2", "c-legal", "Garden Route Abattoir", 3, "250")
        self.matter = LegalMatter.objects.create(
            tenant_id=TENANT, contact_id="c-legal", contact_name="Garden Route Abattoir",
            status=LegalMatter.ACTIVE, approved_at=timezone.now())
        # Closed, and written off.
        _snap("x1", "c-closed", "Closed Co", 40, "400")
        ClosedDebtor.objects.create(tenant_id=TENANT, contact_id="c-closed")
        _snap("w1", "c-wo", "Gone Ltd", 100, "900")
        WriteOffInvoice.objects.create(tenant_id=TENANT, invoice_id="w1", contact_id="c-wo",
                                       amount=Decimal("900"))
        for cid, admin in (("c-active", self.anna), ("c-ho", self.anna),
                           ("c-legal", self.anna), ("c-ret", self.ben)):
            DebtorAllocation.objects.create(tenant_id=TENANT, contact_id=cid, administrator=admin)


class PortfolioStatusTests(BookFixture):
    def test_every_invoice_lands_in_exactly_one_status(self):
        statuses = portfolio.invoice_status_map(TENANT)
        self.assertEqual(statuses["a1"], portfolio.ACTIVE)
        self.assertEqual(statuses["h1"], portfolio.HANDOVER)
        self.assertEqual(statuses["h2"], portfolio.HANDOVER)    # goes with its client
        self.assertEqual(statuses["r1"], portfolio.ACTIVE)      # taken back by hand
        self.assertEqual(statuses["l1"], portfolio.LEGAL)
        self.assertEqual(statuses["l2"], portfolio.LEGAL)       # fresh, but the client is with the attorneys
        self.assertEqual(statuses["x1"], portfolio.CLOSED)
        self.assertNotIn("w1", statuses)                        # written off: out of the book

    def test_snapshot_records_one_row_per_debtor_and_status(self):
        n = portfolio.capture_snapshot(TENANT)
        self.assertEqual(n, 5)
        row = PortfolioSnapshot.objects.get(tenant_id=TENANT, contact_id="c-legal")
        self.assertEqual((row.status, row.total, row.administrator_id),
                         (portfolio.LEGAL, Decimal("5250"), self.anna.id))
        # Re-capturing the same day replaces rather than duplicates.
        portfolio.capture_snapshot(TENANT)
        self.assertEqual(PortfolioSnapshot.objects.filter(tenant_id=TENANT).count(), 5)


class ActionPageTests(BookFixture):
    def _names(self, response):
        return [d["name"] for d in response.context["debtors"]]

    def test_client_with_the_attorneys_leaves_the_action_page_entirely(self):
        self.client.force_login(self.boss)
        r = self.client.get(reverse("xero_aging_report"))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Garden Route Abattoir", self._names(r))
        self.assertIn("Karoo Butchery", self._names(r))
        self.assertIn("Riverside Spar", self._names(r))
        self.assertEqual(r.context["legal_accounts_count"], 1)
        self.assertEqual(r.context["legal_accounts_total"], Decimal("5250"))

    def test_searching_for_it_points_at_the_matter(self):
        self.client.force_login(self.boss)
        r = self.client.get(reverse("xero_aging_report"), {"q": "garden"})
        self.assertEqual(self._names(r), [])
        self.assertEqual([a["matter_id"] for a in r.context["legal_matches"]], [self.matter.id])

    def test_the_note_counts_only_the_clerks_own_book(self):
        self.client.force_login(self.anna)
        self.assertEqual(self.client.get(reverse("xero_aging_report")).context["legal_accounts_count"], 1)
        self.client.force_login(self.ben)
        self.assertEqual(self.client.get(reverse("xero_aging_report")).context["legal_accounts_count"], 0)

    def test_a_matter_brought_back_is_managed_normally_again(self):
        self.matter.status = LegalMatter.CLOSED
        self.matter.save()
        self.client.force_login(self.boss)
        r = self.client.get(reverse("xero_aging_report"))
        self.assertEqual(r.context["legal_accounts_count"], 0)
        # Its aged invoice is past the handover threshold, so the client now
        # sits on the Handover page rather than with the attorneys.
        self.assertEqual(portfolio.invoice_status_map(TENANT)["l2"], portfolio.HANDOVER)


class CategoryTests(BookFixture):
    def test_keyword_match_on_name_and_project_code(self):
        training = DebtorCategory.objects.get(name="Training")
        _snap("t1", "c-train", "Acme Holdings", 10, "100", project="090 - Training Courses")
        resolver = portfolio.CategoryResolver(TENANT)
        self.assertEqual(resolver.resolve("c-legal", "Garden Route Abattoir")[1:], ("Abattoirs", "auto"))
        self.assertEqual(resolver.resolve("c-train", "Acme Holdings", ["090 - Training Courses"])[0],
                         training.id)
        self.assertEqual(resolver.resolve("c-active", "Karoo Butchery"), (None, portfolio.UNCATEGORISED, ""))

    def test_a_hand_set_category_beats_the_keyword(self):
        store = DebtorCategory.objects.get(name="Corporate stores")
        DebtorClassification.objects.create(tenant_id=TENANT, contact_id="c-legal", category=store)
        self.assertEqual(portfolio.CategoryResolver(TENANT).resolve("c-legal", "Garden Route Abattoir")[1:],
                         ("Corporate stores", "manual"))

    def test_only_a_super_admin_can_categorise(self):
        store = DebtorCategory.objects.get(name="Individual stores")
        url = reverse("xero_debtor_category")
        ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        self.client.force_login(self.anna)
        r = self.client.post(url, {"contact_id": "c-active", "category": store.id}, **ajax)
        self.assertEqual(r.status_code, 403)
        self.client.force_login(self.boss)
        r = self.client.post(url, {"contact_id": "c-active", "contact_name": "Karoo Butchery",
                                   "category": store.id}, **ajax)
        self.assertEqual(r.json()["source"], "manual")
        r = self.client.post(url, {"contact_id": "c-active", "category": ""}, **ajax)
        self.assertEqual(r.json()["source"], "")

    def test_bulk_classify_and_filter_the_action_page(self):
        store = DebtorCategory.objects.get(name="Individual stores")
        self.client.force_login(self.boss)
        r = self.client.post(reverse("xero_categories"),
                             {"action": "classify", "cid": ["c-active", "c-ret"], "category": store.id})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(DebtorClassification.objects.filter(category=store).count(), 2)
        r = self.client.get(reverse("xero_aging_report"), {"category": store.id})
        self.assertEqual(sorted(d["name"] for d in r.context["debtors"]),
                         ["Karoo Butchery", "Riverside Spar"])
        self.assertEqual(self.client.get(reverse("xero_categories")).status_code, 200)


class DashboardTests(BookFixture):
    def test_a_clerks_workload_excludes_handover_and_attorney_accounts(self):
        self.client.force_login(self.boss)
        r = self.client.get(reverse("xero_dashboard"))
        self.assertEqual(r.status_code, 200)
        anna = next(a for a in r.context["admin_overview"] if a["id"] == self.anna.id)
        self.assertEqual((anna["companies"], anna["total"]), (1, Decimal("1000")))
        self.assertEqual((anna["ho_companies"], anna["ho_total"]), (1, Decimal("1000")))
        self.assertEqual((anna["legal_companies"], anna["legal_total"]), (1, Decimal("5250")))
        # Written off and closed stay out of the system total.
        self.assertEqual(r.context["system_total"], Decimal("8050"))

    def test_a_clerk_sees_their_split(self):
        self.client.force_login(self.anna)
        r = self.client.get(reverse("xero_dashboard"))
        self.assertEqual(r.context["my_total"], Decimal("1000"))
        self.assertEqual(r.context["my_legal_total"], Decimal("5250"))


class ReportTests(BookFixture):
    def test_collections_recovery_and_escalations(self):
        RecoveredInvoice.objects.create(
            tenant_id=TENANT, invoice_id="a0", contact_id="c-active", contact_name="Karoo Butchery",
            amount=Decimal("500"), credited=True, administrator=self.anna,
            allocated_admin=self.anna, reason=RecoveredInvoice.REASON_COLLECTED)
        self.client.force_login(self.boss)
        r = self.client.get(reverse("xero_reports"))
        self.assertEqual(r.status_code, 200)
        report = r.context["r"]
        anna = next(c for c in report["clerk_rows"] if c["admin_id"] == self.anna.id)
        self.assertEqual(anna["flows"]["collected"], Decimal("500"))
        self.assertEqual(anna["portfolio"]["legal"]["total"], Decimal("5250"))
        # 500 / (500 collected + 0 written off + 7250 still owed on Anna's book)
        self.assertAlmostEqual(anna["recovery"], 500 / 7750 * 100, places=3)
        reasons = {e["cid"]: {k for k, _ in e["reasons"]} for e in report["escalations"]}
        self.assertIn("no_contact", reasons["c-active"])
        self.assertIn("handover_decision", reasons["c-ho"])
        self.assertNotIn("c-closed", reasons)

    def test_follow_up_counts_and_clerk_actions(self):
        CallLog.objects.create(tenant_id=TENANT, invoice_id="a1", contact_id="c-active",
                               contact_name="Karoo Butchery", called_by=self.anna,
                               action_type=CallLog.ACTION_CALL)
        self.client.force_login(self.boss)
        report = self.client.get(reverse("xero_reports")).context["r"]
        anna = next(c for c in report["clerk_rows"] if c["admin_id"] == self.anna.id)
        self.assertEqual(anna["flows"]["calls"], 1)
        self.assertEqual(anna["followed"]["accounts"], 1)
        reasons = {e["cid"]: {k for k, _ in e["reasons"]} for e in report["escalations"]}
        self.assertNotIn("no_contact", reasons.get("c-active", set()))

    def test_movement_uses_the_recorded_opening_balance(self):
        period = period_for("month", timezone.localdate())
        PortfolioSnapshot.objects.create(
            tenant_id=TENANT, day=period.start - timedelta(days=1), contact_id="c-active",
            contact_name="Karoo Butchery", administrator=self.anna, status=portfolio.ACTIVE,
            total=Decimal("1600"), b0_30=Decimal("1600"), invoice_count=2)
        self.client.force_login(self.boss)
        r = self.client.get(reverse("xero_reports"), {"clerk": self.anna.id})
        m = r.context["r"]["movement"]
        self.assertEqual(m["opening"], Decimal("1600"))
        self.assertEqual(m["closing"], Decimal("7250"))
        self.assertEqual(m["new"], m["closing"] - m["opening"] + m["collected"] + m["written_off"])

    def test_every_period_kind_and_the_export_render(self):
        self.client.force_login(self.boss)
        for kind in ("week", "month", "quarter", "year"):
            self.assertEqual(self.client.get(reverse("xero_reports"), {"period": kind}).status_code, 200)
        r = self.client.get(reverse("xero_reports"),
                            {"period": "custom", "from": "2026-01-01", "to": "2026-03-31"})
        self.assertEqual(r.status_code, 200)
        r = self.client.get(reverse("xero_reports"), {"export": "xlsx"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])

    def test_an_administrator_only_sees_their_own_portfolio(self):
        self.client.force_login(self.ben)
        report = self.client.get(reverse("xero_reports"), {"clerk": self.anna.id}).context["r"]
        self.assertEqual(report["scope"].clerk, self.ben.id)
        self.assertEqual([c["admin_id"] for c in report["clerk_rows"]], [self.ben.id])

    def test_lawyers_are_sent_to_their_own_page(self):
        lawyer = User.objects.create_user("law@fsa.test", "x", role="lawyer")
        self.client.force_login(lawyer)
        self.assertRedirects(self.client.get(reverse("xero_reports")), reverse("xero_legal"),
                             fetch_redirect_response=False)


class RecordingTests(BookFixture):
    def test_a_payment_records_whose_portfolio_it_came_in_on(self):
        prev = recovery.capture_open_snapshot(TENANT)
        OpenInvoiceSnapshot.objects.filter(invoice_id="a1").update(amount_due=Decimal("400"))
        recovery.detect_recoveries(TENANT, prev)
        rec = RecoveredInvoice.objects.get(invoice_id="a1")
        # No follow-up logged, so not Anna's credit - but it is on her portfolio.
        self.assertFalse(rec.credited)
        self.assertEqual((rec.allocated_admin_id, rec.amount), (self.anna.id, Decimal("600")))

    def test_a_write_off_records_what_was_owed(self):
        self.client.force_login(self.boss)
        self.client.post(reverse("xero_write_off_invoice"),
                         {"invoice_id": "a1", "contact_id": "c-active", "reason": "Liquidated"},
                         HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(WriteOffInvoice.objects.get(invoice_id="a1").amount, Decimal("1000"))
