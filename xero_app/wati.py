"""WATI WhatsApp Business API client — sends the per-invoice reminders.

Before this module the WhatsApp button opened a wa.me link and the user pressed
send themselves. Going through WATI means the app delivers the message, so a
reminder is logged only when it actually went out.

Two properties of this API shape everything below:

  * WhatsApp only allows FREE-FORM text inside a 24-hour window that opens when
    the customer messages us first. Debtors virtually never have, so a reminder
    must go out as a Meta-APPROVED TEMPLATE. The locally editable wording in
    MessageTemplate cannot be sent verbatim; MessageTemplate.wati_template_name
    names the approved template that actually ships, and the local body stays as
    the operator-facing preview.
  * A 200 from /messageTemplates/send does NOT mean delivered. Meta can reject a
    message after WATI has accepted it (an unapproved template comes back as
    OAuthException #132001) and the send call still reports success:true with an
    empty errors list. Delivery has to be read back from the conversation log,
    which is why send_template_message() verifies before reporting success.

Tokens: the newer "wati_..." personal access tokens only work against the v3
tree (/api/ext/v3) and carry their own tenant, so there is no tenant id in the
path. The older /{tenantId}/api/v1/... shape returns 403 for them.
"""
import logging
import time

import requests
from django.conf import settings

log = logging.getLogger(__name__)

# Statuses WATI reports on a message in the conversation log. Anything in
# _FAILED means Meta rejected it and the customer never saw it.
_FAILED = {"FAILED", "UNDELIVERED"}
_DELIVERED = {"SENT", "DELIVERED", "READ"}


class WatiError(RuntimeError):
    """A send could not be completed. The message text is operator-facing."""


def is_configured():
    """True when a token and base URL are present. Views gate on this so an
    unconfigured account shows the button disabled with a reason, rather than
    offering a send that can only fail."""
    return bool(getattr(settings, "WATI_TOKEN", "") and getattr(settings, "WATI_BASE_URL", ""))


def _url(path):
    return "%s/%s" % (settings.WATI_BASE_URL.rstrip("/"), path.lstrip("/"))


def _headers():
    return {
        "Authorization": "Bearer %s" % settings.WATI_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _timeout():
    return getattr(settings, "WATI_TIMEOUT", 20)


def list_templates():
    """[{name, status, params}] for every template on the account.

    Used by Communication Setup to offer the approved templates in a dropdown
    rather than making someone type the name exactly."""
    if not is_configured():
        return []
    try:
        r = requests.get(_url("messagetemplates"), headers=_headers(),
                         params={"pageSize": 100}, timeout=_timeout())
        r.raise_for_status()
        data = r.json() or {}
    except (requests.RequestException, ValueError) as exc:
        log.warning("WATI: could not list templates: %s", exc)
        return []
    out = []
    for t in data.get("templates") or []:
        out.append({
            "name": t.get("name") or "",
            "status": (t.get("status") or "").lower(),
            "params": [p.get("name") for p in (t.get("custom_params") or []) if p.get("name")],
        })
    return sorted(out, key=lambda t: t["name"].lower())


def approved_template_names():
    """Just the names Meta has approved — the only ones that can actually send."""
    return [t["name"] for t in list_templates() if t["status"] == "approved"]


# Template definitions change only when someone edits them in the WATI console,
# so a short cache keeps a button click from paying for an extra round trip.
_TEMPLATE_CACHE = {"at": 0.0, "by_name": {}}
_TEMPLATE_CACHE_TTL = 300


def template_params(template_name):
    """The variable names the approved template declares, or None if unknown.

    Callers send only these, so mapping a local template to a WATI template with
    a different variable set cannot produce a rejected send."""
    if not template_name:
        return None
    now = time.time()
    if now - _TEMPLATE_CACHE["at"] > _TEMPLATE_CACHE_TTL or not _TEMPLATE_CACHE["by_name"]:
        listed = list_templates()
        if listed:
            _TEMPLATE_CACHE["by_name"] = {t["name"]: t["params"] for t in listed}
            _TEMPLATE_CACHE["at"] = now
    return _TEMPLATE_CACHE["by_name"].get(template_name)


def _latest_message(phone):
    """Newest message in the conversation with `phone`, or None.

    The send response returns a local_message_id that the conversation log does
    not echo back, so identifying our message means watching for a NEW entry to
    appear rather than reading whatever is newest — see _confirm_delivery."""
    try:
        r = requests.get(_url("conversations/%s/messages" % phone), headers=_headers(),
                         params={"pageSize": 1}, timeout=_timeout())
        r.raise_for_status()
        msgs = (r.json() or {}).get("message_list") or []
    except (requests.RequestException, ValueError) as exc:
        log.warning("WATI: could not read conversation for %s: %s", phone, exc)
        return None
    return msgs[0] if msgs else None


def send_template_message(phone, template_name, params, broadcast_name="debtor_reminder"):
    """Send an approved template and confirm Meta accepted it.

    `phone`  international digits, no '+' (e.g. "27716254982")
    `params` {variable_name: value} matching the approved template's variables.

    Returns {"status": "SENT"|"QUEUED", "detail": str}. Raises WatiError when the
    message could not be sent or Meta rejected it, so the caller can avoid
    logging a reminder that never arrived.
    """
    if not is_configured():
        raise WatiError("WhatsApp sending is not configured.")
    if not phone:
        raise WatiError("No WhatsApp number on file for this debtor.")
    if not template_name:
        raise WatiError("This template is not linked to an approved WhatsApp template.")

    # Note which message is newest BEFORE sending. The conversation log does not
    # echo back the local_message_id from the send response, so a new id
    # appearing is the only reliable way to tell our message apart from whatever
    # was already there. Without this, verification reads the previous message
    # and a rejected send inherits its "delivered" status.
    before = _latest_message(phone)
    before_id = (before or {}).get("id")

    # Send exactly the variables the approved template declares. The caller passes
    # every value it has; a template that uses only some of them (or is remapped
    # later to one with a different variable set) still gets a valid payload.
    declared = template_params(template_name)
    values = dict(params or {})
    if declared is not None:
        values = {k: values.get(k, "") for k in declared}

    payload = {
        "template_name": template_name,
        "broadcast_name": broadcast_name,
        "recipients": [{
            "phone_number": phone,
            "custom_params": [{"name": k, "value": "" if v is None else str(v)}
                              for k, v in values.items()],
        }],
    }
    try:
        r = requests.post(_url("messageTemplates/send"), headers=_headers(),
                          json=payload, timeout=_timeout())
    except requests.RequestException as exc:
        raise WatiError("Could not reach WhatsApp: %s" % exc) from exc

    if r.status_code >= 400:
        raise WatiError("WhatsApp rejected the send (HTTP %s): %s"
                        % (r.status_code, (r.text or "")[:300]))
    try:
        body = r.json() or {}
    except ValueError:
        raise WatiError("WhatsApp returned an unreadable response.")

    if not body.get("success"):
        raise WatiError("WhatsApp did not accept the message: %s" % (body.get("message") or body))
    # Per-recipient errors travel separately from the top-level success flag.
    for rec in body.get("recipients") or []:
        if rec.get("errors"):
            raise WatiError("WhatsApp rejected %s: %s" % (phone, rec["errors"]))

    return _confirm_delivery(phone, before_id)


def _confirm_delivery(phone, before_id):
    """Read the conversation log back to catch a Meta-side rejection.

    The send call reports success before Meta has looked at the message, so a
    template that is not approved still returns 200 and only shows up as FAILED
    here a moment later.

    Only a message whose id differs from `before_id` is ours. Judging whatever is
    newest would let a rejected send inherit the status of the message before it
    — the failure mode this guards against is reporting a delivery that did not
    happen, which would log a reminder nobody sent. If our message has not
    surfaced by the time the attempts run out we report QUEUED rather than
    inventing a verdict we did not observe."""
    attempts = getattr(settings, "WATI_VERIFY_ATTEMPTS", 3)
    delay = getattr(settings, "WATI_VERIFY_DELAY", 1.0)
    for i in range(max(1, attempts)):
        if i:
            time.sleep(delay)
        msg = _latest_message(phone)
        if not msg or msg.get("id") == before_id:
            continue          # ours has not appeared in the log yet
        status = (msg.get("status_string") or msg.get("status") or "").upper()
        if status in _FAILED:
            detail = msg.get("failed_detail") or "WhatsApp could not deliver the message."
            raise WatiError(_friendly_failure(detail))
        if status in _DELIVERED:
            return {"status": "SENT", "detail": status}
    # Accepted by WATI, no verdict yet — genuinely in flight.
    return {"status": "QUEUED", "detail": "Accepted by WhatsApp; delivery not yet confirmed."}


def _friendly_failure(detail):
    """Turn Meta's raw error into something an operator can act on."""
    text = detail or ""
    if "132001" in text:
        return ("WhatsApp rejected the template — it is not approved on the "
                "WhatsApp account. Check Communication Setup.")
    if "131026" in text or "not a valid whatsapp user" in text.lower():
        return "That number is not registered on WhatsApp."
    if "131047" in text:
        return "WhatsApp needs the customer to reply before a free-form message can be sent."
    return "WhatsApp could not deliver the message: %s" % text[:200]
