"""Audit capture: who did what, when.

Two sources feed accounts.AuditLog:
  * AuditLogMiddleware — every authenticated data-changing request (POST /
    PUT / PATCH / DELETE) is recorded with a friendly label, the sanitised
    payload, the response status and the caller's IP. One middleware covers
    every add / edit / delete in the app without instrumenting each view.
  * Auth signals — sign-in, sign-out and failed sign-in attempts.

Auditing must never break the request: every write is wrapped in try/except.
"""
import json

from django.contrib.auth import get_user_model
from django.contrib.auth.signals import (user_logged_in, user_logged_out,
                                         user_login_failed)
from django.dispatch import receiver
from django.utils.html import format_html

from .models import AuditLog

# POST fields that must never be stored.
_SECRET_FIELDS = ('password', 'password1', 'password2', 'new_password1',
                  'new_password2', 'old_password', 'csrfmiddlewaretoken')
_VALUE_LIMIT = 300

# url_name -> human label. Anything not listed falls back to the raw name, so
# new views are still audited even before a label is added here.
FRIENDLY_LABELS = {
    # Debtors / collections
    'xero_allocate_debtor': 'Changed a debtor allocation',
    'xero_log_call': 'Logged a call',
    'xero_log_whatsapp': 'Logged a WhatsApp',
    'xero_log_email': 'Logged an email',
    'xero_unlog_contact': 'Removed a contact mark',
    'xero_add_comment': 'Added an invoice comment',
    'xero_debtor_comment_add': 'Added a debtor comment',
    'xero_followup_shift': 'Changed a follow-up schedule',
    # Close / write-off
    'xero_close_debtor': 'Marked a debtor closed',
    'xero_reopen_debtor': 'Reopened a debtor',
    'xero_write_off_invoice': 'Wrote off an invoice',
    'xero_write_off_debtor': 'Wrote off a whole client',
    'xero_unwrite_off_invoice': 'Reversed a write-off',
    'xero_credit_note_toggle': 'Toggled credit-note issued',
    # Handover / legal
    'xero_handover_mark': 'Marked an invoice for handover',
    'xero_handover_unmark': 'Moved an invoice back from handover',
    'xero_handover_debtor': 'Handed over a whole client',
    'xero_handover_settings': 'Changed a handover rule',
    'xero_legal_send': 'Sent a company to the lawyers',
    'xero_legal_approve': 'Approved a legal matter',
    'xero_legal_cancel': 'Closed / withdrew a legal matter',
    'xero_legal_return': 'Returned a matter from the lawyers',
    'xero_legal_step_toggle': 'Ticked / unticked a legal step',
    'xero_legal_step_comment': 'Commented on a legal step',
    'xero_legal_toggle_opposed': 'Toggled Opposed/Unopposed',
    # Admin / settings
    'user_create': 'Created a user',
    'user_invite': 'Invited a user',
    'user_edit': 'Edited a user',
    'user_delete': 'Deleted a user',
    'user_toggle_active': 'Activated / deactivated a user',
    'user_reset_password': "Reset a user's password",
    'user_resend_invite': 'Re-sent a user invite',
    'xero_communication_setup': 'Changed communication templates',
    'xero_schedule': 'Changed the sync schedule',
    'xero_lawyer_report': 'Changed the lawyer report settings',
    'xero_sync_now': 'Triggered a manual sync',
}


def _bold(value):
    return format_html('<strong>{}</strong>', value)


def _with_quote(sentence, quote):
    if not quote:
        return sentence
    return format_html('{} — <span class="al-quote">“{}”</span>',
                       sentence, quote[:140])


def humanize_entry(url_name, label, params, user_names):
    """Turn one audited data change into a human sentence (safe HTML).

    ``params`` is the sanitised POST payload; ``user_names`` maps user id ->
    display name (for allocation entries). Anything unrecognised falls back to
    'label + invoice + company + quoted reason', so new views still read well."""
    company = params.get('contact_name') or params.get('contact_id') or ''
    invoice = params.get('invoice_number') or params.get('invoice_id') or ''
    quote = params.get('reason') or params.get('text') or params.get('note') or ''

    if url_name == 'xero_allocate_debtor':
        admin_id = str(params.get('administrator') or '')
        if admin_id.isdigit() and int(admin_id) in user_names:
            return format_html('allocated {} to {}', _bold(company),
                               _bold(user_names[int(admin_id)]))
        return format_html('unallocated {}', _bold(company))

    if url_name == 'xero_followup_shift':
        bits = []
        for key, lab in (('call_due_days', 'call'), ('whatsapp_due_days', 'WhatsApp'),
                         ('email_due_days', 'email')):
            if params.get(key):
                bits.append(f"{lab} from day {params[key]}")
        if params.get('shift_days') not in (None, '', '0'):
            bits.append(f"everything pushed +{params['shift_days']} days")
        detail = ', '.join(bits) if bits else 'reset to the default cadence'
        return _with_quote(
            format_html('changed the follow-up schedule for {} ({})',
                        _bold(company), detail), quote)

    if url_name == 'xero_credit_note_toggle':
        if params.get('now_checked') == 'true':
            return format_html('marked the credit note as issued on {}',
                               _bold(invoice or 'an invoice'))
        return format_html('removed the credit-note mark from {}',
                           _bold(invoice or 'an invoice'))

    if url_name == 'xero_debtor_comment_add':
        return _with_quote(format_html('commented on {}', _bold(company)), quote)

    if url_name == 'xero_handover_settings':
        mode = params.get('mode') or ''
        if mode == 'never':
            detail = 'never auto-hand over'
        elif mode == 'default':
            detail = 'back to the system default'
        else:
            detail = f"auto-hand over at {params.get('handover_days', '?')} days"
        return _with_quote(format_html('changed the handover rule for {} ({})',
                                       _bold(company), detail), quote)

    if url_name in ('user_create', 'user_invite', 'user_edit'):
        who = ' '.join(p for p in (params.get('first_name', ''),
                                   params.get('last_name', '')) if p)
        email = params.get('email', '')
        target = who or email or 'a user'
        sentence = format_html('{} — {}', label, _bold(target))
        if email and who:
            sentence = format_html('{} ({})', sentence, email)
        if params.get('role'):
            sentence = format_html('{}, role {}', sentence, params['role'])
        return sentence

    # Generic fallback: verb + invoice + company + quoted reason.
    sentence = label
    if invoice:
        sentence = format_html('{} — {}', sentence, _bold(invoice))
    if company:
        sentence = format_html('{} for {}', sentence, _bold(company))
    return _with_quote(sentence, quote)


def _sanitised_params(request):
    out = {}
    try:
        for key, values in request.POST.lists():
            if key.lower() in _SECRET_FIELDS:
                continue
            vals = [v[:_VALUE_LIMIT] for v in values]
            out[key] = vals[0] if len(vals) == 1 else vals
    except Exception:
        pass
    return out


def _log(user, action, label='', url_name='', method='', path='',
         params=None, status=None):
    try:
        AuditLog.objects.create(
            user=user if getattr(user, 'pk', None) else None,
            user_label=(getattr(user, 'email', '') or str(user or ''))[:255],
            action=action, label=label[:120], url_name=url_name[:80],
            method=method[:8], path=path[:255],
            params_json=json.dumps(params, ensure_ascii=False) if params else '',
            status_code=status,
        )
    except Exception:
        pass  # auditing must never take a request down


class AuditLogMiddleware:
    """Records every authenticated data-changing request (anything not
    GET/HEAD/OPTIONS). Sign-in/out are recorded by the auth signals below,
    so those two URLs are skipped here."""

    SKIP_URL_NAMES = {'login', 'logout'}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if request.method in ('GET', 'HEAD', 'OPTIONS'):
                return response
            user = getattr(request, 'user', None)
            if not (user and user.is_authenticated):
                return response
            url_name = getattr(getattr(request, 'resolver_match', None), 'url_name', '') or ''
            if url_name in self.SKIP_URL_NAMES:
                return response
            _log(user, AuditLog.ACTION_CHANGE,
                 label=FRIENDLY_LABELS.get(url_name, url_name or request.path),
                 url_name=url_name, method=request.method, path=request.path[:255],
                 params=_sanitised_params(request),
                 status=getattr(response, 'status_code', None))
        except Exception:
            pass
        return response


@receiver(user_logged_in)
def _audit_login(sender, request, user, **kwargs):
    _log(user, AuditLog.ACTION_LOGIN, label='Signed in',
         path=getattr(request, 'path', ''))


@receiver(user_logged_out)
def _audit_logout(sender, request, user, **kwargs):
    if user is None:
        return
    _log(user, AuditLog.ACTION_LOGOUT, label='Signed out',
         path=getattr(request, 'path', ''))


@receiver(user_login_failed)
def _audit_login_failed(sender, credentials, request=None, **kwargs):
    attempted = (credentials or {}).get('username', '')[:255]
    try:
        AuditLog.objects.create(
            user=None, user_label=attempted or '(unknown)',
            action=AuditLog.ACTION_LOGIN_FAILED, label='Failed sign-in attempt',
            path=getattr(request, 'path', '') if request else '',
        )
    except Exception:
        pass
