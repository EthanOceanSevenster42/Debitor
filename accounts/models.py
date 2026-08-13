from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class Role(models.TextChoices):
    SUPER_ADMIN = 'super_admin', 'Super Admin'
    ADMINISTRATOR = 'administrator', 'Administrator'
    LAWYER = 'lawyer', 'Lawyer'


class UserManager(BaseUserManager):
    """User manager keyed on email instead of username."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError('An email address is required.')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', Role.SUPER_ADMIN)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    email = models.EmailField('email address', unique=True)
    # No default role: a user has NO access until a Super Admin assigns one
    # (the invite / add-user forms always set it). Blank = no access.
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        blank=True,
        default='',
    )
    # Per-user grant: full working access to the Lawyers page (tick steps,
    # comment, upload) for someone whose role wouldn't otherwise allow it —
    # e.g. an Administrator who also works the legal matters.
    legal_access = models.BooleanField(
        'Lawyers page access',
        default=False,
        help_text='Can work matters on the Lawyers page (in addition to their role).',
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        db_table = 'accounts_user'
        ordering = ['first_name', 'last_name', 'email']

    def __str__(self):
        full = self.get_full_name()
        return full or self.email

    @property
    def is_super_admin(self):
        return self.role == Role.SUPER_ADMIN

    @property
    def is_administrator(self):
        return self.role == Role.ADMINISTRATOR

    @property
    def is_lawyer(self):
        return self.role == Role.LAWYER

    @property
    def is_pending_invite(self):
        """An invited user who hasn't accepted yet: inactive with no usable
        password set."""
        return not self.is_active and not self.has_usable_password()


class AuditLog(models.Model):
    """One row per audited event: every data-changing request (adds, edits,
    deletions — captured by ``accounts.audit.AuditLogMiddleware``) plus
    sign-ins, sign-outs and failed sign-in attempts (captured by auth
    signals). Viewed on the Super-Admin-only Audit Log page."""
    ACTION_CHANGE = 'change'
    ACTION_LOGIN = 'login'
    ACTION_LOGOUT = 'logout'
    ACTION_LOGIN_FAILED = 'login_failed'
    ACTION_CHOICES = [
        (ACTION_CHANGE, 'Data change'),
        (ACTION_LOGIN, 'Signed in'),
        (ACTION_LOGOUT, 'Signed out'),
        (ACTION_LOGIN_FAILED, 'Failed sign-in'),
    ]

    user = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='audit_entries')
    # Snapshot of who acted, kept even if the user row is later deleted.
    user_label = models.CharField(max_length=255, blank=True, default='')
    action = models.CharField(max_length=15, choices=ACTION_CHOICES,
                              default=ACTION_CHANGE, db_index=True)
    # Human label of what happened ("Wrote off an invoice") — from the URL-name
    # map in accounts.audit; falls back to the raw URL name.
    label = models.CharField(max_length=120, blank=True, default='')
    url_name = models.CharField(max_length=80, blank=True, default='')
    method = models.CharField(max_length=8, blank=True, default='')
    path = models.CharField(max_length=255, blank=True, default='')
    # Sanitised POST payload (no passwords/CSRF, values truncated), as JSON.
    params_json = models.TextField(blank=True, default='')
    status_code = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'created_at'])]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.user_label}: {self.label or self.action}"
