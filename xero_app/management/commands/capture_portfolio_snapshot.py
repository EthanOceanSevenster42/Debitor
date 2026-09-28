"""Record where the debtor book stands today, for the performance reports.

The hourly sync already does this after every rebuild, so this is only needed
to take a snapshot without syncing (e.g. straight after deploying, so the
reports have a starting point today) or if the sync is paused.

    python manage.py capture_portfolio_snapshot
"""
from django.core.management.base import BaseCommand

from xero_app import portfolio
from xero_app.models import XeroConnection


class Command(BaseCommand):
    help = "Capture today's portfolio snapshot (balances per debtor, clerk and status)"

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Only this tenant_id", default=None)

    def handle(self, *args, **opts):
        qs = XeroConnection.objects.all()
        if opts.get("tenant"):
            qs = qs.filter(tenant_id=opts["tenant"])
        for conn in qs:
            n = portfolio.capture_snapshot(conn.tenant_id)
            self.stdout.write(self.style.SUCCESS(
                f"[{conn.tenant_name or conn.tenant_id}] {n} portfolio row(s) captured"))
