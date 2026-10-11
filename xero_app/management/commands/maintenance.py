"""Switch maintenance mode on or off.

    python manage.py maintenance on
    python manage.py maintenance on --message "Upgrading the reports" --until "14:00 today"
    python manage.py maintenance off
    python manage.py maintenance status

While on, everyone except signed-in Super Admins sees the maintenance page.
"""
import json
import os

from django.core.management.base import BaseCommand

from xero_app import maintenance


class Command(BaseCommand):
    help = "Turn the maintenance page on or off for everyone except Super Admins"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["on", "off", "status"])
        parser.add_argument("--message", default="", help="Shown on the page, e.g. what is being done")
        parser.add_argument("--until", default="", help='When it should be back, e.g. "14:00 today"')

    def handle(self, *args, **opts):
        path = maintenance.flag_path()
        if opts["action"] == "on":
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"message": opts["message"], "until": opts["until"]}, f)
            self.stdout.write(self.style.WARNING("Maintenance mode ON - users now see the maintenance page."))
        elif opts["action"] == "off":
            if os.path.exists(path):
                os.remove(path)
            self.stdout.write(self.style.SUCCESS("Maintenance mode OFF - the system is open again."))
        else:
            state = maintenance.status()
            self.stdout.write("ON" + (f" - {state['message']}" if state["message"] else "") if state else "OFF")
