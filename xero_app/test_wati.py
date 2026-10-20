"""What an operator is told when WATI refuses the system's login."""
from unittest import mock

from django.test import SimpleTestCase, override_settings

from . import wati


@override_settings(WATI_TOKEN="t", WATI_BASE_URL="https://wati.test/api/ext/v3")
class WatiRefusedLoginTests(SimpleTestCase):

    def _send(self, status, text=""):
        with mock.patch.object(wati, "_latest_message", return_value=None), \
             mock.patch.object(wati, "template_params", return_value=None), \
             mock.patch.object(wati.requests, "post", return_value=mock.Mock(status_code=status, text=text)):
            with self.assertRaises(wati.WatiError) as caught:
                wati.send_template_message("27820000000", "debtor_reminder", {})
        return str(caught.exception)

    def test_a_refused_token_says_so_instead_of_a_bare_code(self):
        for status in (401, 403):
            message = self._send(status)
            self.assertIn("WhatsApp is not connected", message)
            self.assertIn("renew the WATI API token", message)
            self.assertIn(f"HTTP {status}", message)

    def test_other_errors_still_give_the_reason(self):
        self.assertIn("no reason given", self._send(500))
        self.assertIn("bad template", self._send(400, "bad template"))
