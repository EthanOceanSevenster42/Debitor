"""The 404 page, and maintenance mode."""
import os
import tempfile

from django.core.management import call_command
from django.test import override_settings

from .tests import BookFixture

FLAG = os.path.join(tempfile.mkdtemp(prefix="fsa-maint-"), "maintenance.on")


@override_settings(MAINTENANCE_FLAG=FLAG)
class StatusPageTests(BookFixture):

    def tearDown(self):
        if os.path.exists(FLAG):
            os.remove(FLAG)
        super().tearDown()

    def test_an_unknown_address_shows_the_404_page(self):
        r = self.client.get("/no-such-page/")
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "We couldn't find that page", status_code=404)

    def test_maintenance_mode_shows_the_page_to_everyone_but_super_admins(self):
        call_command("maintenance", "on", message="Upgrading the reports", until="14:00")
        self.client.force_login(self.anna)
        r = self.client.get("/xero/dashboard/")
        self.assertEqual(r.status_code, 503)
        self.assertContains(r, "We'll be back shortly", status_code=503)
        self.assertContains(r, "Upgrading the reports", status_code=503)
        self.assertContains(r, "14:00", status_code=503)
        self.client.logout()
        self.assertEqual(self.client.get("/login/").status_code, 200)        # Super Admins can still sign in
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get("/xero/dashboard/").status_code, 200)
        call_command("maintenance", "off")
        self.client.force_login(self.anna)
        self.assertEqual(self.client.get("/xero/dashboard/").status_code, 200)
