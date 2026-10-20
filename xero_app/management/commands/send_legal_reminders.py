"""Send the legal diary's reminders: payments due and missed, follow-ups, and
each person's daily list. Each reminder goes out once.

The hourly sync already runs this; use the command to run it on its own.

    python manage.py send_legal_reminders
"""
from django.core.management.base import BaseCommand

from xero_app import notifications


class Command(BaseCommand):
    help = "Send the legal diary reminders (payments due or missed, follow-ups, daily lists)"

    def handle(self, *args, **opts):
        n = notifications.send_legal_diary_reminders()
        self.stdout.write(self.style.SUCCESS(f"{n} legal diary reminder(s) sent"))
