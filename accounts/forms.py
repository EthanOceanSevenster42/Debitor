from django import forms
from django.contrib.auth.forms import UserCreationForm, SetPasswordForm

from .models import User, Role


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
    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'role', 'legal_access', 'is_active')
        widgets = {'is_active': forms.CheckboxInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        self._style()
        # checkboxes shouldn't get the text-input styling
        self.fields['is_active'].widget.attrs['class'] = ''
        self.fields['legal_access'].widget.attrs['class'] = ''


class AdminSetPasswordForm(StyledFormMixin, SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()
