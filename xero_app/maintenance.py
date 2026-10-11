"""Maintenance mode: while switched on, everyone sees the maintenance page.

Switched on and off with ``python manage.py maintenance on|off``, which writes
or removes a small flag file (settings.MAINTENANCE_FLAG) - a file rather than a
database row, so the page still shows while the database itself is being
migrated or restored. Super Admins can still sign in and use the system, to
check things before switching it off; the login page and static files stay
reachable so they can.
"""
import json
import os

from django.conf import settings
from django.template.loader import render_to_string
from django.http import HttpResponse

_ALWAYS_OPEN = ("/static/", "/login/", "/logout/", "/favicon.ico")


def flag_path():
    return str(getattr(settings, "MAINTENANCE_FLAG", ""))


def status():
    """None when off, else {"message", "until"} from the flag file."""
    path = flag_path()
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {"message": data.get("message", ""), "until": data.get("until", "")}


class MaintenanceModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        state = status()
        if state is None or request.path.startswith(_ALWAYS_OPEN):
            return self.get_response(request)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and getattr(user, "is_super_admin", False):
            return self.get_response(request)
        body = render_to_string("maintenance.html", state, request=request)
        response = HttpResponse(body, status=503)
        response["Retry-After"] = "120"
        return response
