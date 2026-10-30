from django import forms
from django.contrib.auth.forms import UserCreationForm, SetPasswordForm

from .models import User, Role, WRITE_OFF_GROUPS


class StyledFormMixin:
    """Apply the shared .form-control styling to every widget."""

    def _style(self):
        for field in self.fields.values():
            css = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = (css + ' form-control').strip()


class UserCreateForm(StyledFormMixin, UserCreationForm):
    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'role', 'legal_access')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        self.fields['role'].choices = Role.choices
        self._style()
        # checkbox shouldn't get the text-input styling
        self.fields['legal_access'].widget.attrs['class'] = ''


class UserInviteForm(StyledFormMixin, forms.ModelForm):
    """Invite a new user: capture their name, email and role. No password — the
    invitee sets that themselves via the emailed invite link."""

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'role', 'legal_access')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        self.fields['role'].choices = Role.choices
        self._style()
        self.fields['legal_access'].widget.attrs['class'] = ''


class UserEditForm(StyledFormMixin, forms.ModelForm):
    write_off_groups = forms.MultipleChoiceField(
        label=User._meta.get_field('write_off_groups').verbose_name,
        help_text=User._meta.get_field('write_off_groups').help_text,
        choices=[(key, label) for key, label, _codes in WRITE_OFF_GROUPS],
        widget=forms.CheckboxSelectMultiple(), required=False)

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'role', 'legal_access', 'also_manages',
                  'write_off_groups', 'is_active')
        widgets = {'is_active': forms.CheckboxInput(),
                   'also_manages': forms.CheckboxSelectMultiple()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        self._style()
        # checkboxes shouldn't get the text-input styling
        self.fields['is_active'].widget.attrs['class'] = ''
        self.fields['legal_access'].widget.attrs['class'] = ''
        # Only people who hold a debtor book can be covered, and not oneself.
        others = User.objects.filter(role__in=[Role.ADMINISTRATOR, Role.SUPER_ADMIN], is_active=True)
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        self.fields['also_manages'].queryset = others.order_by('first_name', 'last_name')
        self.fields['also_manages'].widget.attrs['class'] = ''
        self.fields['write_off_groups'].widget.attrs['class'] = ''
        if self.instance.pk and not self.is_bound:
            self.initial['write_off_groups'] = [k for k in self.instance.write_off_groups.split(',') if k]

    def clean_write_off_groups(self):
        return ','.join(self.cleaned_data.get('write_off_groups') or [])


class AdminSetPasswordForm(StyledFormMixin, SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()
