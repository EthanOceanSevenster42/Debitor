"""Alert FSA when a Letter of Demand is due for hand delivery by an inspector.

The hourly sync already runs this; use the command to run it on its own.

    python manage.py send_lod_delivery_reminders
"""
from django.core.management.base import BaseCommand

from xero_app import notifications


class Command(BaseCommand):
    help = "Notify FSA of Letters of Demand due for hand delivery (7 days after sending)"

    def handle(self, *args, **opts):
        n = notifications.send_due_lod_delivery_reminders()
        self.stdout.write(self.style.SUCCESS(f"{n} hand-delivery alert(s) sent"))
