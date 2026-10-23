"""Xero API client that works against a saved XeroConnection (no Django session needed).

Used by the sync_xero management command and the on-demand refresh view.
"""
import os
import time
import requests
from datetime import timedelta
from django.utils import timezone

XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_API_BASE = "https://api.xero.com/api.xro/2.0"


class XeroDailyLimitError(RuntimeError):
    """Raised when Xero's 5,000-calls/day cap is exhausted (resets ~24h later).

    We fail fast on this rather than retrying, because retries can't succeed
    today and each one would burn more of the (already spent) daily budget.
    """


class XeroCallBudgetReached(RuntimeError):
    """Raised when our self-imposed per-run call budget is hit.

    We deliberately run at HALF of Xero's published limits. This stops the
    current run cleanly (its work so far is kept) and the next run resumes,
    so we never approach the real daily cap.
    """


# We run at half of Xero's published limits, on purpose:
#   - Minute limit 60/min  -> we pace at 30/min  = one call every 2.0s.
#   - Concurrent limit 5   -> we stay sequential (1 in flight).
#   - Daily limit 5,000    -> enforced by the caller via a per-day call budget.
# Pacing centrally in _get() (rather than ad-hoc sleeps) keeps every call,
# including retries, under the same throttle.
_REQUEST_PAUSE_SECONDS = 2.0
_DEFAULT_RETRY_AFTER = 15


class _Pacer:
    """Throttles outgoing Xero calls and enforces a per-run call budget.

    Configured once per sync run via reset(); every _get() attempt passes
    through acquire(), which sleeps to hold the 30/min pace and raises
    XeroCallBudgetReached once the run's budget is spent.
    """

    def __init__(self):
        self.min_interval = _REQUEST_PAUSE_SECONDS
        self.max_calls = None  # None => unlimited (budget enforced elsewhere)
        self.calls = 0
        self._last_ts = 0.0

    def reset(self, max_calls=None, min_interval=None):
        self.calls = 0
        self._last_ts = 0.0
        self.max_calls = max_calls
        if min_interval is not None:
            self.min_interval = min_interval

    def acquire(self):
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise XeroCallBudgetReached(
                f"Per-run Xero call budget ({self.max_calls}) reached; "
                f"remaining work resumes on the next run."
            )
        wait = self.min_interval - (time.monotonic() - self._last_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_ts = time.monotonic()
        self.calls += 1


# Module-level pacer shared by all calls in a process. The sync command resets it
# at the start of each run; on-demand views leave it unbudgeted (max_calls=None).
pacer = _Pacer()


def _refresh_access_token(connection):
    """Use the saved refresh token to get a new access token. Updates the connection in place."""
    client_id = os.environ.get("XERO_CLIENT_ID", "")
    client_secret = os.environ.get("XERO_CLIENT_SECRET", "")
    r = requests.post(
        XERO_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": connection.refresh_token},
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    connection.access_token = data["access_token"]
    connection.refresh_token = data.get("refresh_token", connection.refresh_token)
    connection.token_expires_at = timezone.now() + timedelta(seconds=int(data.get("expires_in", 1800)))
    connection.save()
    return connection.access_token


def _ensure_token(connection):
    if connection.token_expires_at <= timezone.now() + timedelta(seconds=60):
        return _refresh_access_token(connection)
    return connection.access_token


def _headers(connection):
    return {
        "Authorization": f"Bearer {_ensure_token(connection)}",
        "xero-tenant-id": connection.tenant_id,
        "Accept": "application/json",
    }


def _get(connection, path, extra_headers=None, max_429_retries=20, raw=False):
    """GET with 401 (one refresh) + 429 (Retry-After) handling. Returns parsed JSON
    (or the raw bytes, with ``raw``) or raises."""
    url = f"{XERO_API_BASE}/{path}"
    refreshed_once = False
    for attempt in range(max_429_retries + 1):
        # Throttle to 30/min and honour the per-run budget. Retries (401/429) pass
        # through here too, so they're paced and counted just like first attempts.
        pacer.acquire()
        headers = _headers(connection)
        if extra_headers:
            headers.update(extra_headers)
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code == 401 and not refreshed_once:
            _refresh_access_token(connection)
            refreshed_once = True
            continue
        if r.status_code == 429:
            problem = (r.headers.get("X-Rate-Limit-Problem") or "").lower()
            if problem == "day" or r.headers.get("X-DayLimit-Remaining") == "0":
                retry_after = r.headers.get("Retry-After", "?")
                raise XeroDailyLimitError(
                    f"Xero daily API limit (5,000/day) reached. Resets in ~{retry_after}s."
                )
            try:
                wait = float(r.headers.get("Retry-After") or _DEFAULT_RETRY_AFTER)
            except (TypeError, ValueError):
                wait = _DEFAULT_RETRY_AFTER
            time.sleep(min(wait + 1, 65))
            continue
        r.raise_for_status()
        return r.content if raw else r.json()
    raise RuntimeError(f"Xero rate limit retries exhausted for {path}")


def _modified_since_header(modified_since):
    """Xero's If-Modified-Since expects a UTC datetime with no offset suffix."""
    if not modified_since:
        return None
    return {"If-Modified-Since": modified_since.strftime("%Y-%m-%dT%H:%M:%S")}


def fetch_open_invoices(connection):
    """Sequential, rate-limit-friendly pull of every open ACCREC invoice.

    Full detail (not summaryOnly) so each invoice carries its LineItems, and with
    them the Tracking categories used to derive the project code. Sequential is
    deliberate: this runs on a schedule, not on a user click, so minimizing
    rate-limit pressure matters more than wall-clock time. Pacing/budget are
    enforced centrally in _get().
    """
    all_invoices = []
    page = 1
    while True:
        data = _get(
            connection,
            f'Invoices?where=Type=="ACCREC"&Statuses=AUTHORISED,SUBMITTED&page={page}',
        )
        items = data.get("Invoices") or []
        if not items:
            break
        all_invoices.extend(items)
        if len(items) < 100:
            break
        page += 1
    return all_invoices


def extract_project_code(inv, category_name="Tracking"):
    """Project code for an invoice = the given Xero Tracking category's option(s)
    across its line items, e.g. "060 - Processed Meat Products". Comma-joined and
    de-duplicated when an invoice's lines span more than one option.
    """
    codes = []
    for li in (inv.get("LineItems") or []):
        for t in (li.get("Tracking") or []):
            if (t.get("Name") or "") == category_name:
                opt = (t.get("Option") or "").strip()
                if opt and opt not in codes:
                    codes.append(opt)
    return ", ".join(codes)


def fetch_contact(connection, contact_id):
    """Return the full Xero contact record for one debtor (or None).

    One API call per contact (use sparingly / cache the result).
    """
    data = _get(connection, f"Contacts/{contact_id}")
    contacts = data.get("Contacts") or []
    return contacts[0] if contacts else None


def clean_contact(c):
    """Reduce a raw Xero contact to the fields the details panel stores/shows.

    This is the canonical shape persisted in ContactDetail.data_json, used by
    both the sync (proactive caching) and the on-demand contact view.
    """
    def _phone(p):
        num = " ".join(x for x in (p.get("PhoneCountryCode"), p.get("PhoneAreaCode"),
                                   p.get("PhoneNumber")) if x).strip()
        return {"type": (p.get("PhoneType") or "").title(), "number": num}

    phones = [_phone(p) for p in (c.get("Phones") or []) if (p.get("PhoneNumber") or "").strip()]

    def _addr(a):
        lines = [a.get(k) for k in ("AddressLine1", "AddressLine2", "AddressLine3", "AddressLine4")]
        loc = ", ".join(x for x in (a.get("City"), a.get("Region"), a.get("PostalCode"),
                                    a.get("Country")) if x)
        out = [x for x in lines if x]
        if loc:
            out.append(loc)
        return {"type": (a.get("AddressType") or "").title(), "lines": out}

    addresses = [_addr(a) for a in (c.get("Addresses") or [])
                 if any(a.get(k) for k in ("AddressLine1", "City", "PostalCode", "Country"))]

    persons = [{
        "name": f"{p.get('FirstName', '')} {p.get('LastName', '')}".strip(),
        "email": p.get("EmailAddress") or "",
    } for p in (c.get("ContactPersons") or [])]

    pt = (c.get("PaymentTerms") or {}).get("Sales") or {}
    term_label = {
        "DAYSAFTERBILLDATE": "days after invoice date",
        "DAYSAFTERBILLMONTH": "days after end of invoice month",
        "OFCURRENTMONTH": "of the current month",
        "OFFOLLOWINGMONTH": "of the following month",
    }.get(pt.get("Type"), pt.get("Type") or "")
    payment_terms = f"{pt.get('Day')} {term_label}".strip() if pt.get("Day") is not None else ""

    return {
        "name": c.get("Name") or "",
        "account_number": c.get("AccountNumber") or "",
        "email": c.get("EmailAddress") or "",
        "status": c.get("ContactStatus") or "",
        "tax_number": c.get("TaxNumber") or "",
        "default_currency": c.get("DefaultCurrency") or "",
        "phones": phones,
        "addresses": addresses,
        "contact_persons": persons,
        "payment_terms": payment_terms,
    }


def fetch_online_invoice_url(connection, invoice_id):
    """Return the public 'view online' permalink for one invoice (or "").

    Calls Xero's GET Invoices/{id}/OnlineInvoice (1 API call). The returned URL
    (https://in.xero.com/...) is a stable permalink, so callers cache it and
    never need to refetch it.
    """
    data = _get(connection, f"Invoices/{invoice_id}/OnlineInvoice")
    items = data.get("OnlineInvoices") or []
    return (items[0].get("OnlineInvoiceUrl") or "") if items else ""


def fetch_invoice_pdf(connection, invoice_id):
    """The invoice as Xero prints it, as PDF bytes (1 API call)."""
    return _get(connection, f"Invoices/{invoice_id}", extra_headers={"Accept": "application/pdf"}, raw=True)


def fetch_invoice_history(connection, invoice_id):
    """Return the raw Xero History & Notes records for one invoice.

    Each record looks like {Changes, DateUTC, User, Details}. Xero logs an entry
    when an invoice is emailed/sent, so this is the source of the reminder
    lifecycle. One API call per invoice (use sparingly / cache the result).
    """
    data = _get(connection, f"Invoices/{invoice_id}/History")
    return data.get("HistoryRecords") or []


def iter_ar_invoices(connection, modified_since=None, start_page=1):
    """Yield pages of AR (ACCREC) invoices, one list per Xero page.

    Streaming (rather than accumulating) keeps memory low and lets the caller
    persist each page as it arrives, so a large backfill is resilient to a
    late failure. summaryOnly keeps payloads light while still returning
    invoice-level totals, amount due/paid, status, contact and dates.

    If `modified_since` is given, Xero only returns invoices created or changed
    since then (incremental sync) - this is what keeps routine refreshes fast.
    """
    extra_headers = _modified_since_header(modified_since)
    page = start_page
    while True:
        data = _get(
            connection,
            f'Invoices?where=Type=="ACCREC"&summaryOnly=true&page={page}',
            extra_headers=extra_headers,
        )
        items = data.get("Invoices") or []
        if not items:
            break
        yield page, items
        if len(items) < 100:
            break
        page += 1
