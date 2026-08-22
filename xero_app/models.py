from django.conf import settings
from django.db import models


class XeroConnection(models.Model):
    tenant_id = models.CharField(max_length=64, primary_key=True)
    tenant_name = models.CharField(max_length=255, blank=True)
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_expires_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)
    # Set only when a FULL invoice backfill has completed. Until then the sync
    # stays in full-backfill mode (so an interrupted backfill resumes correctly);
    # afterwards it becomes the incremental watermark.
    invoices_synced_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.tenant_name or self.tenant_id}"


class OpenInvoiceSnapshot(models.Model):
    tenant_id = models.CharField(max_length=64, db_index=True)
    invoice_id = models.CharField(max_length=64)
    invoice_number = models.CharField(max_length=64, blank=True)
    contact_id = models.CharField(max_length=64, db_index=True)
    contact_name = models.CharField(max_length=255, blank=True)
    contact_email = models.CharField(max_length=255, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    days_past_due = models.IntegerField(default=0)
    bucket = models.CharField(max_length=20)
    amount_due = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, blank=True)
    status = models.CharField(max_length=20)
    # Xero "Tracking" category option(s) on the invoice's line items, e.g.
    # "060 - Processed Meat Products". The project/department code the invoice is
    # linked to; comma-joined when an invoice spans more than one.
    project_code = models.CharField(max_length=255, blank=True, default="")
    # Xero "Inspector" tracking category option(s) on the invoice's line items,
    # e.g. "010 - C Nel" — who inspected for this invoice. Comma-joined when an
    # invoice's lines span more than one inspector.
    inspector = models.CharField(max_length=255, blank=True, default="")
    # Xero's public "view online" permalink for this invoice (https://in.xero.com/...).
    # Denormalized here from OnlineInvoiceLink each rebuild so views read it directly;
    # the durable copy lives in OnlineInvoiceLink so it survives the snapshot rebuild.
    online_url = models.CharField(max_length=255, blank=True, default="")
    # When Xero last emailed a reminder for this invoice (derived from the cached
    # History & Notes), and the cadence stage of that reminder.
    last_reminder_at = models.DateTimeField(null=True, blank=True)
    last_reminder_stage = models.CharField(max_length=64, blank=True, default="")
    synced_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("tenant_id", "invoice_id")]
        indexes = [
            models.Index(fields=["tenant_id", "bucket"]),
            models.Index(fields=["tenant_id", "contact_id"]),
        ]


class Invoice(models.Model):
    """Local copy of every AR (ACCREC) invoice, refreshed by the hourly sync.

    Backs the Debtors listing page so it reads from SQL Server instead of
    calling Xero live on each request.
    """
    tenant_id = models.CharField(max_length=64, db_index=True)
    invoice_id = models.CharField(max_length=64)
    invoice_number = models.CharField(max_length=64, blank=True)
    contact_id = models.CharField(max_length=64, blank=True, db_index=True)
    contact_name = models.CharField(max_length=255, blank=True)
    contact_email = models.CharField(max_length=255, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    amount_due = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, blank=True)
    status = models.CharField(max_length=20, db_index=True)
    updated_date_utc = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("tenant_id", "invoice_id")]
        indexes = [
            models.Index(fields=["tenant_id", "status"]),
            models.Index(fields=["tenant_id", "due_date"]),
        ]
        ordering = ["-due_date", "-invoice_date"]


class DebtorNotice(models.Model):
    """A popup waiting for one user.

    Raised when a Super Admin comments on a debtor that is allocated to somebody
    else: the allocated administrator is told, wherever they happen to be in the
    app. Rows are kept after dismissal so the trail shows what was raised and
    when it was seen.
    """
    tenant_id = models.CharField(max_length=64, db_index=True)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="debtor_notices",
    )
    actor_name = models.CharField(max_length=255, blank=True)
    actor_role = models.CharField(max_length=64, blank=True)
    contact_id = models.CharField(max_length=255, blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "seen_at"])]

    def __str__(self):
        return f"to {self.recipient}: {self.contact_name} ({self.text[:30]})"


class DebtorAllocation(models.Model):
    """Allocates a debtor (Xero contact) to an administrator for payment follow-up.

    Lives in its own table so the allocation survives the hourly snapshot rebuild.
    Keyed by the same debtor id the report groups on (contact_id, or the contact
    name when no id is present). One administrator per company.
    """
    tenant_id = models.CharField(max_length=64, db_index=True)
    contact_id = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    administrator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="allocated_debtors"
    )
    assigned_by = models.CharField(max_length=255, blank=True)
    assigned_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("tenant_id", "contact_id")]

    def __str__(self):
        return f"{self.contact_name or self.contact_id} -> {self.administrator}"


class CallLog(models.Model):
    """Records a contact attempt against a debtor about a specific invoice.

    ACTION_TYPE distinguishes the channel: a phone call, a WhatsApp reminder, or
    an email. Each channel is an independent follow-up task — logging one clears
    only that channel's prompt for a few days (CALL_SUPPRESS_DAYS); the other
    channels stay outstanding until they're actioned in their own right."""
    ACTION_CALL = "call"
    ACTION_WHATSAPP = "whatsapp"
    ACTION_EMAIL = "email"
    ACTION_CHOICES = [
        (ACTION_CALL, "Call"),
        (ACTION_WHATSAPP, "WhatsApp"),
        (ACTION_EMAIL, "Email"),
    ]

    tenant_id = models.CharField(max_length=64, db_index=True)
    invoice_id = models.CharField(max_length=64, db_index=True, blank=True)
    invoice_number = models.CharField(max_length=64, blank=True)
    contact_id = models.CharField(max_length=255, db_index=True)
    contact_name = models.CharField(max_length=255, blank=True)
    # Which channel this contact attempt used. Existing rows default to "call";
    # the data migration retags historical WhatsApp rows from their note text.
    action_type = models.CharField(
        max_length=10, choices=ACTION_CHOICES, default=ACTION_CALL, db_index=True
    )
    called_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="calls_logged",
    )
    called_by_name = models.CharField(max_length=255, blank=True)
    note = models.CharField(max_length=255, blank=True)
    called_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-called_at"]


class ClosedDebtor(models.Model):
    """A debtor (Xero contact) the user has marked 'closed'.

    Lives in its own table so the flag survives the hourly snapshot rebuild.
    Keyed by the same debtor id the report groups on (contact_id, or the
    contact name when no id is present).
    """
    tenant_id = models.CharField(max_length=64, db_index=True)
    contact_id = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    note = models.CharField(max_length=255, blank=True)
    closed_by = models.CharField(max_length=255, blank=True)
    closed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("tenant_id", "contact_id")]
        ordering = ["-closed_at"]

    def __str__(self):
        return self.contact_name or self.contact_id


class HandoverInvoice(models.Model):
    """An invoice on the Handover page.

    A row exists when an invoice has been touched in the handover flow — either
    manually marked by an administrator, or once an admin has clicked
    "Activate handover" on an auto-listed (over-65-days) one. Invoices that are
    >65 days past due AND whose contact is NOT in HandoverExclusion are also
    surfaced on the page automatically, even without a row here. The reasons and
    follow-up notes are kept as InvoiceComment rows so the invoice lifecycle
    shows the full trail.

    SOURCE distinguishes between automatic (debt aged past 65 days) and manual
    (admin chose to send a younger invoice down the handover path).
    """
    SOURCE_AUTO = "auto"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = [(SOURCE_AUTO, "Auto (>65 days)"), (SOURCE_MANUAL, "Manual")]

    tenant_id = models.CharField(max_length=64, db_index=True)
    invoice_id = models.CharField(max_length=64, db_index=True)
    invoice_number = models.CharField(max_length=64, blank=True)
    contact_id = models.CharField(max_length=255, blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    marked_by = models.CharField(max_length=255, blank=True)
    marked_at = models.DateTimeField(auto_now_add=True)
    # When an administrator clicks "Activate handover" the invoice becomes visible
    # to users with the Lawyer role; until then they don't see it.
    activated_for_lawyer = models.BooleanField(default=False)
    activated_by = models.CharField(max_length=255, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("tenant_id", "invoice_id")]
        ordering = ["-marked_at"]

    def __str__(self):
        return f"{self.invoice_number or self.invoice_id} ({self.contact_name})"


class HandoverExclusion(models.Model):
    """A contact excluded from automatic handover (typically because there is an
    active payment arrangement). Their >65-day invoices stay on the Debtors
    Action page and don't surface on the Handover page unless an administrator
    manually marks an individual invoice for handover anyway.
    """
    tenant_id = models.CharField(max_length=64, db_index=True)
    contact_id = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    note = models.CharField(max_length=255, blank=True)
    excluded_by = models.CharField(max_length=255, blank=True)
    excluded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("tenant_id", "contact_id")]
        ordering = ["-excluded_at"]

    def __str__(self):
        return self.contact_name or self.contact_id


class WriteOffInvoice(models.Model):
    """An invoice the user has marked 'written off'.

    Per-invoice flag (vs ClosedDebtor which is per-debtor). Lives in its own
    table so it survives the hourly snapshot rebuild. The reason given when
    written off, the reversal, and any follow-up notes are captured as
    InvoiceComment rows so they show in the invoice's lifecycle alongside
    other activity.
    """
    tenant_id = models.CharField(max_length=64, db_index=True)
    invoice_id = models.CharField(max_length=64, db_index=True)
    invoice_number = models.CharField(max_length=64, blank=True)
    contact_id = models.CharField(max_length=255, blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    written_off_by = models.CharField(max_length=255, blank=True)
    written_off_at = models.DateTimeField(auto_now_add=True)
    # Ticked (Super Admin only) once the matching credit note has been issued
    # in Xero for this written-off invoice.
    credit_note_issued = models.BooleanField(default=False)
    credit_note_by = models.CharField(max_length=255, blank=True, default="")
    credit_note_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("tenant_id", "invoice_id")]
        ordering = ["-written_off_at"]

    def __str__(self):
        return f"{self.invoice_number or self.invoice_id} ({self.contact_name})"


class ContactDetail(models.Model):
    """Cached Xero contact details for a debtor (address, phones, contact people,
    payment terms, etc.). Fetched on demand (1 Xero call per contact) and cached
    so repeat views are instant and don't burn the daily API budget.

    data_json is the cleaned contact dict used by the contact-details panel.
    """
    tenant_id = models.CharField(max_length=64, db_index=True)
    contact_id = models.CharField(max_length=64)
    data_json = models.TextField(blank=True, default="{}")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("tenant_id", "contact_id")]


class DebtorComment(models.Model):
    """A comment on a DEBTOR (company) rather than a single invoice — the
    per-debtor mini chat. Shown in the debtor's expanded statement on the
    Debtors Action, Handover and Write-off pages, so the whole team sees the
    same running conversation about the client wherever they meet them.
    Keyed by the same debtor id the listing pages group on (contact_id, or
    the contact name when no id is present)."""
    tenant_id = models.CharField(max_length=64, db_index=True)
    contact_id = models.CharField(max_length=255, db_index=True)
    contact_name = models.CharField(max_length=255, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="debtor_comments",
    )
    author_name = models.CharField(max_length=255, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["tenant_id", "contact_id"])]

    def __str__(self):
        return f"{self.contact_name or self.contact_id}: {self.text[:40]}"


class InvoiceComment(models.Model):
    """A user comment on an invoice, with a chosen date/time and optional
    document attachments. Surfaced inside the invoice lifecycle."""
    tenant_id = models.CharField(max_length=64, db_index=True)
    invoice_id = models.CharField(max_length=64, db_index=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invoice_comments",
    )
    author_name = models.CharField(max_length=255, blank=True)
    comment_at = models.DateTimeField()
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["comment_at"]


class InvoiceCommentAttachment(models.Model):
    comment = models.ForeignKey(InvoiceComment, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="invoice_comments/%Y/%m/")
    original_name = models.CharField(max_length=255, blank=True)
    # What kind of document this is (e.g. "Proof of payment", "Statement"),
    # captured at upload so the filing reads clearly. Blank on older rows.
    nature = models.CharField(max_length=120, blank=True, default="")
    uploaded_at = models.DateTimeField(auto_now_add=True)


class InvoiceHistory(models.Model):
    """Cached Xero 'History & Notes' for a single invoice (its email/activity
    lifecycle). Fetched on demand (1 Xero call per invoice) and cached here so
    repeat views are instant and don't burn the daily API budget.

    events_json is a JSON list of {date, user, action, details, is_email}.
    """
    tenant_id = models.CharField(max_length=64, db_index=True)
    invoice_id = models.CharField(max_length=64)
    events_json = models.TextField(blank=True, default="[]")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("tenant_id", "invoice_id")]


class OnlineInvoiceLink(models.Model):
    """Cached public 'view online' permalink for one invoice (Xero's
    GET Invoices/{id}/OnlineInvoice -> OnlineInvoiceUrl).

    The URL is a stable permalink, so it's fetched once (1 Xero call) and reused
    forever. Lives in its own table so it survives the open-invoice snapshot
    rebuild; the snapshot just denormalizes a copy from here on each rebuild.
    """
    tenant_id = models.CharField(max_length=64, db_index=True)
    invoice_id = models.CharField(max_length=64)
    url = models.CharField(max_length=255)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("tenant_id", "invoice_id")]


DEFAULT_WA_TEMPLATE = (
    "Hi {name}, this is a friendly reminder from FSA. "
    "Invoice {invoice_number} for R {amount} is {days_overdue}. "
    "Could you let us know when we can expect payment? Thanks."
)


class WhatsAppTemplate(models.Model):
    """Editable message template used by the per-invoice WhatsApp button.

    A single row holds the template. Placeholders use {name}, {invoice_number},
    {amount}, {days_past_due}, {days_overdue} and {due_date} (rendered
    server-side before the wa.me URL is built)."""
    template_text = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=255, blank=True, default="")

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def effective_template(self):
        return self.template_text or DEFAULT_WA_TEMPLATE


DEFAULT_EMAIL_SUBJECT = "FSA reminder: invoice {invoice_number}"
DEFAULT_EMAIL_BODY = (
    "Dear {name},\n\n"
    "This is a friendly reminder from FSA that invoice {invoice_number} for "
    "R {amount} is {days_overdue}.\n\n"
    "Could you please let us know when we can expect payment? If you have already "
    "made payment, kindly send us the proof of payment so we can allocate it.\n\n"
    "Thank you,\nFSA Accounts Team"
)


class EmailTemplate(models.Model):
    """Editable subject + body the per-invoice Email button pre-fills into the
    user's mail client (mailto:). Mirrors WhatsAppTemplate.

    A single row holds the template. Placeholders use {name}, {invoice_number},
    {amount}, {days_past_due}, {days_overdue} and {due_date} (rendered
    server-side before the mailto: link is built)."""
    subject_text = models.TextField(blank=True, default="")
    body_text = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=255, blank=True, default="")

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def effective_subject(self):
        return self.subject_text or DEFAULT_EMAIL_SUBJECT

    @property
    def effective_body(self):
        return self.body_text or DEFAULT_EMAIL_BODY


class MessageTemplate(models.Model):
    """A named, editable reminder template for a contact channel (email or
    WhatsApp). Multiple per channel; exactly one is the default. Managed on the
    Communication Setup page and offered as a dropdown next to the per-invoice
    Email / WhatsApp buttons. Placeholders: {name}, {invoice_number}, {amount},
    {days_past_due}, {days_overdue}, {due_date}. (Supersedes the single-row
    WhatsAppTemplate / EmailTemplate, whose contents are seeded in as the first
    default of each channel by the data migration.)"""
    CHANNEL_EMAIL = "email"
    CHANNEL_WHATSAPP = "whatsapp"
    CHANNEL_CHOICES = [(CHANNEL_EMAIL, "Email"), (CHANNEL_WHATSAPP, "WhatsApp")]

    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, db_index=True)
    name = models.CharField(max_length=120)
    # subject is used by the email channel only; WhatsApp ignores it.
    subject = models.TextField(blank=True, default="")
    body = models.TextField(blank=True, default="")
    # WhatsApp only: the Meta-APPROVED template on the WATI account that this
    # wording maps to. WhatsApp refuses free-form text to someone who has not
    # messaged us in the last 24 hours, so a reminder can only go out as an
    # approved template — `body` above is the operator-facing preview, and this
    # names what actually ships. Blank keeps the old behaviour for this template:
    # a wa.me link the user sends by hand.
    wati_template_name = models.CharField(max_length=200, blank=True, default="")
    is_default = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["channel", "sort_order", "name"]

    def __str__(self):
        return f"{self.get_channel_display()}: {self.name}"

    @property
    def sends_via_api(self):
        """True when this template can be delivered by the app itself."""
        return bool(self.channel == self.CHANNEL_WHATSAPP and self.wati_template_name)


class SystemSetting(models.Model):
    """Single-row system-wide settings.

    go_live_date: the date the system went live. Invoices ISSUED before this date
    are never flagged as missed call / WhatsApp / email (they pre-date the tool,
    so there was no chance to log contact in time). They can still be called,
    WhatsApped and emailed normally."""
    go_live_date = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=255, blank=True, default="")

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SyncSchedule(models.Model):
    """User-configurable schedule for the Xero -> SQL sync.

    The Windows Scheduled Task keeps firing sync_xero hourly so that any
    configured time can be caught; the gate at the top of sync_xero consults
    this row to decide whether to actually run, skip, or wait. One row controls
    the whole installation.
    """
    INTERVAL = "interval"
    FIXED = "fixed"
    MODE_CHOICES = [(INTERVAL, "Every N hours"), (FIXED, "At specific times of day")]
    INTERVAL_CHOICES = (1, 2, 3, 4, 6, 8, 12, 24)

    enabled = models.BooleanField(default=True)
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default=INTERVAL)
    interval_hours = models.IntegerField(default=1)
    # Comma-separated 24h times in server-local time, e.g. "08:00,12:00,16:00".
    fixed_times = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=255, blank=True, default="")

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def parsed_fixed_times(self):
        out = []
        for raw in (self.fixed_times or "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                hh, mm = raw.split(":")
                hh, mm = int(hh), int(mm)
            except (ValueError, TypeError):
                continue
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                out.append((hh, mm))
        return sorted(set(out))

    def should_skip(self, now=None, last_success=None):
        """Returns a reason string if this run should be skipped, else None.

        last_success is the last successful SyncRun (or None) — passed in so we
        don't have a circular import on the model layer.
        """
        from django.utils import timezone as tz
        from datetime import timedelta
        if not self.enabled:
            return "auto-sync disabled in scheduler settings"
        now = now or tz.now()
        last_finished = last_success.finished_at if last_success else None

        if self.mode == self.INTERVAL:
            if not last_finished:
                return None
            interval = timedelta(hours=max(1, self.interval_hours))
            elapsed = now - last_finished
            if elapsed < interval:
                return (f"scheduled — last run was {_humanize_td(elapsed)} ago; "
                        f"next due in {_humanize_td(interval - elapsed)}")
            return None

        # FIXED-times mode.
        times = self.parsed_fixed_times()
        if not times:
            return None
        now_local = tz.localtime(now)
        slots = sorted(now_local.replace(hour=h, minute=m, second=0, microsecond=0)
                       for h, m in times)
        past = [s for s in slots if s <= now_local]
        if not past:
            return f"scheduled — first run today at {slots[0].strftime('%H:%M')}"
        most_recent = past[-1]
        if last_finished and tz.localtime(last_finished) >= most_recent:
            future = [s for s in slots if s > now_local]
            label = future[0].strftime("%H:%M") if future else "tomorrow"
            return (f"scheduled — already ran for the {most_recent.strftime('%H:%M')} "
                    f"slot; next at {label}")
        return None


def _humanize_td(td):
    total = int(td.total_seconds())
    h, m = total // 3600, (total % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


class LawyerReportConfig(models.Model):
    """Singleton schedule + on/off switch for the weekly lawyer progress report.

    A Windows Scheduled Task fires `manage.py send_lawyer_report` frequently;
    `is_due()` gates whether a report actually goes out — so the whole schedule
    (frequency, day, time) is configurable from the UI without touching Task
    Scheduler. Recipients live in ReportRecipient."""
    DAILY = "daily"
    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY = "monthly"
    FREQUENCY_CHOICES = [
        (DAILY, "Every day"),
        (WEEKLY, "Every week"),
        (FORTNIGHTLY, "Every 2 weeks"),
        (MONTHLY, "Every month"),
    ]
    DOW_CHOICES = [(0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
                   (4, "Friday"), (5, "Saturday"), (6, "Sunday")]
    _MIN_GAP_DAYS = {DAILY: 1, WEEKLY: 6, FORTNIGHTLY: 13, MONTHLY: 27}
    _PERIOD_DAYS = {DAILY: 1, WEEKLY: 7, FORTNIGHTLY: 14, MONTHLY: 30}

    enabled = models.BooleanField(default=False)
    frequency = models.CharField(max_length=12, choices=FREQUENCY_CHOICES, default=WEEKLY)
    day_of_week = models.IntegerField(default=0)    # 0=Mon..6=Sun (weekly/fortnightly)
    day_of_month = models.IntegerField(default=1)   # 1..28 (monthly)
    send_hour = models.IntegerField(default=7)      # 0..23, server-local time
    send_minute = models.IntegerField(default=0)    # 0..59
    last_sent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=255, blank=True, default="")

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def period_days(self):
        return self._PERIOD_DAYS.get(self.frequency, 7)

    @property
    def send_time_label(self):
        return f"{self.send_hour:02d}:{self.send_minute:02d}"

    def is_due(self, now=None):
        """True when a report should be sent right now: enabled, today's send
        time has passed, today matches the configured day, and we haven't already
        sent within this period."""
        from django.utils import timezone as tz
        if not self.enabled:
            return False
        now = now or tz.now()
        local = tz.localtime(now)
        slot = local.replace(hour=self.send_hour, minute=self.send_minute,
                             second=0, microsecond=0)
        if local < slot:
            return False
        if self.frequency in (self.WEEKLY, self.FORTNIGHTLY):
            if local.weekday() != self.day_of_week:
                return False
        elif self.frequency == self.MONTHLY:
            if local.day != self.day_of_month:
                return False
        if self.last_sent_at:
            gap = (local.date() - tz.localtime(self.last_sent_at).date()).days
            if gap < self._MIN_GAP_DAYS.get(self.frequency, 6):
                return False
        return True


class ReportRecipient(models.Model):
    """An email address that receives the weekly lawyer report. Managed (add /
    remove / toggle) from the report settings page."""
    email = models.EmailField()
    name = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return self.email


class SyncRun(models.Model):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    STATUS_CHOICES = [(s, s) for s in (PENDING, RUNNING, SUCCESS, FAILED)]

    tenant_id = models.CharField(max_length=64, db_index=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=RUNNING)
    invoice_count = models.IntegerField(default=0)
    # Number of Xero API calls this run consumed. Summed across the calendar day
    # to enforce the self-imposed half-of-daily-limit budget.
    api_calls = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]


# Global default: invoices auto-land on the Handover page once this many days
# past due, unless a per-debtor HandoverSetting overrides it.
DEFAULT_HANDOVER_DAYS = 65


class HandoverSetting(models.Model):
    """Per-debtor override of when invoices auto-land on the Handover page.

    Replaces the old binary HandoverExclusion. When auto_handover is False the
    debtor's invoices never auto-hand over (the old 'exclude' case — e.g. a
    payment arrangement). Otherwise they auto-hand over at handover_days days past
    due. No row for a debtor = the global default (DEFAULT_HANDOVER_DAYS)."""
    tenant_id = models.CharField(max_length=64, db_index=True)
    contact_id = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    auto_handover = models.BooleanField(default=True)
    handover_days = models.IntegerField(default=DEFAULT_HANDOVER_DAYS)
    # Push this debtor's whole follow-up cadence (call/WhatsApp/email prompts and
    # the missed flags) later by this many days — e.g. for a payment arrangement,
    # so the admin isn't prompted, and the debtor isn't chased, too early. 0 = the
    # normal cadence. Set by administrators (a collections tool).
    cadence_shift_days = models.IntegerField(default=0)
    # Per-channel follow-up start: how many days past due before this debtor is
    # due to be called / WhatsApped / emailed. NULL = the system default
    # (outreach.CALL_MIN). Set by administrators on the Debtors Action page.
    call_due_days = models.IntegerField(null=True, blank=True)
    whatsapp_due_days = models.IntegerField(null=True, blank=True)
    email_due_days = models.IntegerField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    set_by = models.CharField(max_length=255, blank=True)
    set_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("tenant_id", "contact_id")]
        ordering = ["contact_name"]

    def __str__(self):
        return self.contact_name or self.contact_id


class LegalMatter(models.Model):
    """A debtor (company) handed over to the lawyers for the LBINC collections
    process. One per company. Created when an admin clicks 'Send to lawyers'
    (status PENDING); becomes ACTIVE — and visible on the Lawyers page — once any
    administrator approves it. The attorney then works the workflow: choosing a
    route (summons / application for payment), toggling Unopposed/Opposed, ticking
    steps and commenting on them."""
    PENDING = "pending"
    ACTIVE = "active"
    CLOSED = "closed"
    STATUS_CHOICES = [(PENDING, "Pending approval"), (ACTIVE, "Active"), (CLOSED, "Closed")]

    tenant_id = models.CharField(max_length=64, db_index=True)
    contact_id = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    # The workflow runs sequentially: Collections, then Summons, then Application
    # for payment. Each litigation route has its own Unopposed/Opposed state
    # (False = Unopposed, True = Opposed/Defended), toggleable if the debtor
    # changes strategy. (`track`/`opposed` are retained, unused, from the earlier
    # single-route design.)
    track = models.CharField(max_length=20, blank=True, default="")
    opposed = models.BooleanField(default=False)
    summons_opposed = models.BooleanField(default=False)
    application_opposed = models.BooleanField(default=False)
    sent_by = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.CharField(max_length=255, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("tenant_id", "contact_id")]
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.contact_name or self.contact_id} ({self.get_status_display()})"


class LegalStep(models.Model):
    """Completion state of one workflow step for a matter (keyed by the catalogue
    step key in legal_workflow.py)."""
    matter = models.ForeignKey(LegalMatter, on_delete=models.CASCADE, related_name="step_states")
    step_key = models.CharField(max_length=60)
    done = models.BooleanField(default=False)
    done_by = models.CharField(max_length=255, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("matter", "step_key")]


class LegalStepComment(models.Model):
    """An attorney's comment on a workflow step (including the Variations step)."""
    matter = models.ForeignKey(LegalMatter, on_delete=models.CASCADE, related_name="step_comments")
    step_key = models.CharField(max_length=60, db_index=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="legal_step_comments",
    )
    author_name = models.CharField(max_length=255, blank=True)
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class LegalStepCommentAttachment(models.Model):
    """A document an attorney attached to a workflow-step comment (pleadings,
    court orders, etc.). Surfaced on the matter page and in the company file."""
    comment = models.ForeignKey(LegalStepComment, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="legal_comments/%Y/%m/")
    original_name = models.CharField(max_length=255, blank=True)
    # Nature of the document (e.g. "Summons", "Court order", "Letter of demand"),
    # captured at upload so the filing reads clearly. Blank on older rows.
    nature = models.CharField(max_length=120, blank=True, default="")
    uploaded_at = models.DateTimeField(auto_now_add=True)


class RecoveredInvoice(models.Model):
    """A payment received against an outstanding invoice, detected each sync by
    comparing the open-invoice snapshot to the previous one (a drop in amount_due
    = money in). One row per payment event, so partial payments each get a row.

    `credited` records whether the payment counts as recovered money. Credited
    recoveries are attributed by `reason`:
      * REASON_COLLECTED — recovered in active collections (allocated + followed up
        via a logged call/WhatsApp/email, not yet at handover). `administrator` is
        the allocated admin; it counts toward THEIR recovered total.
      * REASON_COLLECTED_LEGAL — recovered while approved/active with the lawyers
        (partial payments included). Credited to the LAWYERS as a team total;
        `administrator` is left null (it is NOT an administrator's credit).
    Payments that land while merely on the handover page (not yet approved with the
    lawyers), or with no allocation, are recorded but left uncredited."""
    REASON_COLLECTED = "collected"            # credited — recovered during collections
    REASON_COLLECTED_LEGAL = "collected_legal"  # credited — recovered while with the lawyers
    REASON_HANDOVER_LEGAL = "handover_legal"  # on handover page, not yet active legal — uncredited
    REASON_NO_FOLLOWUP = "no_followup"
    REASON_UNALLOCATED = "unallocated"
    REASON_CHOICES = [
        (REASON_COLLECTED, "Collected by admin"),
        (REASON_COLLECTED_LEGAL, "Recovered while with the lawyers"),
        (REASON_HANDOVER_LEGAL, "In handover (pre-lawyers)"),
        (REASON_NO_FOLLOWUP, "No follow-up logged"),
        (REASON_UNALLOCATED, "Debtor not allocated"),
    ]

    tenant_id = models.CharField(max_length=64, db_index=True)
    invoice_id = models.CharField(max_length=64, db_index=True)
    invoice_number = models.CharField(max_length=64, blank=True)
    contact_id = models.CharField(max_length=255, blank=True, db_index=True)
    contact_name = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credited = models.BooleanField(default=False, db_index=True)
    administrator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="recoveries",
    )
    administrator_name = models.CharField(max_length=255, blank=True)
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, default=REASON_COLLECTED)
    days_past_due = models.IntegerField(default=0)
    recovered_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-recovered_at"]
        indexes = [
            models.Index(fields=["tenant_id", "administrator", "recovered_at"]),
        ]

    def __str__(self):
        return f"{self.invoice_number or self.invoice_id}: {self.amount}"
