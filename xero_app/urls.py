from django.urls import path
from django.views.generic import RedirectView
from . import views

urlpatterns = [
    path("login/", views.xero_login, name="xero_login"),
    path("callback/", views.xero_callback, name="xero_callback"),
    path("dashboard/", views.xero_dashboard, name="xero_dashboard"),
    path("aging/", views.xero_aging_report, name="xero_aging_report"),
    path("closed/", views.xero_closed_debtors, name="xero_closed_debtors"),
    path("write-offs/", views.xero_write_offs, name="xero_write_offs"),
    path("debtor/close/", views.xero_close_debtor, name="xero_close_debtor"),
    path("debtor/reopen/", views.xero_reopen_debtor, name="xero_reopen_debtor"),
    path("invoice/write-off/", views.xero_write_off_invoice, name="xero_write_off_invoice"),
    path("debtor/write-off/", views.xero_write_off_debtor, name="xero_write_off_debtor"),
    path("invoice/credit-note/", views.xero_credit_note_toggle, name="xero_credit_note_toggle"),
    path("debtor/comment/", views.xero_debtor_comment_add, name="xero_debtor_comment_add"),
    path("invoice/unwrite-off/", views.xero_unwrite_off_invoice, name="xero_unwrite_off_invoice"),
    path("handover/", views.xero_handover, name="xero_handover"),
    path("handover/overrides/", views.xero_handover_overrides, name="xero_handover_overrides"),
    path("invoice/handover/", views.xero_handover_mark, name="xero_handover_mark"),
    path("debtor/handover/", views.xero_handover_debtor, name="xero_handover_debtor"),
    path("invoice/handover/unmark/", views.xero_handover_unmark, name="xero_handover_unmark"),
    path("debtor/handover-settings/", views.xero_handover_settings, name="xero_handover_settings"),
    path("debtor/followup-shift/", views.xero_followup_shift, name="xero_followup_shift"),
    # Legal process (LBINC workflow)
    path("legal/", views.xero_legal, name="xero_legal"),
    path("legal/send/", views.xero_legal_send, name="xero_legal_send"),
    path("legal/approve/", views.xero_legal_approve, name="xero_legal_approve"),
    path("legal/cancel/", views.xero_legal_cancel, name="xero_legal_cancel"),
    path("legal/return/", views.xero_legal_return, name="xero_legal_return"),
    path("legal/<int:matter_id>/", views.xero_legal_matter, name="xero_legal_matter"),
    path("legal/<int:matter_id>/timeline/", views.xero_legal_timeline, name="xero_legal_timeline"),
    path("legal/<int:matter_id>/opposed/", views.xero_legal_toggle_opposed, name="xero_legal_toggle_opposed"),
    path("legal/<int:matter_id>/step/", views.xero_legal_step_toggle, name="xero_legal_step_toggle"),
    path("legal/<int:matter_id>/comment/", views.xero_legal_step_comment, name="xero_legal_step_comment"),
    # Filing / archive (Super Admin)
    path("filing/", views.xero_filing, name="xero_filing"),
    path("filing/company/", views.xero_filing_company, name="xero_filing_company"),
    path("debtor/statement/", views.xero_debtor_statement, name="xero_debtor_statement"),
    path("debtor/allocate/", views.xero_allocate_debtor, name="xero_allocate_debtor"),
    # Popup notices (polled from every page)
    path("notifications/", views.xero_notifications, name="xero_notifications"),
    path("notices/", views.xero_notices, name="xero_notices"),
    path("notices/seen/", views.xero_notice_seen, name="xero_notice_seen"),
    path("notices/delete/", views.xero_notice_delete, name="xero_notice_delete"),
    path("debtor/log-call/", views.xero_log_call, name="xero_log_call"),
    path("invoice/<str:invoice_id>/email-sent/", views.xero_log_email, name="xero_log_email"),
    path("invoice/<str:invoice_id>/unlog-contact/", views.xero_unlog_contact, name="xero_unlog_contact"),
    path("invoice/<str:invoice_id>/history/", views.xero_invoice_history, name="xero_invoice_history"),
    path("invoice/<str:invoice_id>/comment/", views.xero_add_comment, name="xero_add_comment"),
    path("invoice/<str:invoice_id>/report/", views.xero_invoice_report, name="xero_invoice_report"),
    path("invoice/<str:invoice_id>/online/", views.xero_invoice_online, name="xero_invoice_online"),
    # The only WhatsApp route: the app sends the reminder itself and logs it only
    # if it went out. (The old whatsapp-sent route logged a wa.me link the user
    # pressed send on by hand; the button no longer opens WhatsApp at all.)
    path("invoice/<str:invoice_id>/whatsapp-send/", views.xero_send_whatsapp, name="xero_send_whatsapp"),
    path("company-report/", views.xero_company_report, name="xero_company_report"),
    path("manual/", views.xero_manual, name="xero_manual"),
    path("schedule/", views.xero_schedule, name="xero_schedule"),
    path("lawyer-report/", views.xero_lawyer_report, name="xero_lawyer_report"),
    path("lawyer-report/preview/", views.xero_lawyer_report_preview, name="xero_lawyer_report_preview"),
    path("communication-setup/", views.xero_communication_setup, name="xero_communication_setup"),
    # Back-compat: the old single-channel template pages were merged into
    # Communication Setup — redirect any stale bookmarks there.
    path("whatsapp-template/", RedirectView.as_view(pattern_name="xero_communication_setup", permanent=False)),
    path("email-template/", RedirectView.as_view(pattern_name="xero_communication_setup", permanent=False)),
    path("contact/<str:contact_id>/", views.xero_contact_detail, name="xero_contact_detail"),
    path("aging/refresh/", views.xero_refresh_now, name="xero_refresh_now"),
    path("export/", views.xero_export_all, name="xero_export_all"),
]
