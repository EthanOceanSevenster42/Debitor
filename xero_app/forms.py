from django import forms
from django.core.validators import validate_email

from .models import LegalClientInfo


def _digits(value):
    return "".join(c for c in (value or "") if c.isdigit())


def valid_sa_id(number):
    """A South African ID number: 13 digits ending in a Luhn check digit."""
    if len(number) != 13 or not number.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


class LegalClientInfoForm(forms.ModelForm):
    class Meta:
        model = LegalClientInfo
        fields = ["registered_name", "trading_name", "entity_type", "registration_number",
                  "id_number", "vat_number", "directors", "domicilium_address", "postal_address",
                  "contact_person", "contact_phone", "contact_email", "notes"]
        widgets = {
            "directors": forms.Textarea(attrs={"rows": 3}),
            "domicilium_address": forms.Textarea(attrs={"rows": 3}),
            "postal_address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        hints = {
            "registration_number": "e.g. 2015/123456/07",
            "id_number": "13 digits",
            "vat_number": "10 digits, starts with 4",
            "registered_name": "As on the CIPC registration",
        }
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
            if name in hints:
                field.widget.attrs["placeholder"] = hints[name]

    def clean_id_number(self):
        raw = (self.cleaned_data.get("id_number") or "").strip()
        if not raw:
            return ""
        number = _digits(raw)
        if not valid_sa_id(number):
            raise forms.ValidationError("That is not a valid 13-digit South African ID number - "
                                        "please check it.")
        return number

    def clean_vat_number(self):
        raw = (self.cleaned_data.get("vat_number") or "").strip()
        if not raw:
            return ""
        number = _digits(raw)
        if len(number) != 10 or not number.startswith("4"):
            raise forms.ValidationError("A South African VAT number is 10 digits and starts with 4.")
        return number

    def clean_registration_number(self):
        return " ".join((self.cleaned_data.get("registration_number") or "").split()).upper()

    def clean_contact_email(self):
        raw = (self.cleaned_data.get("contact_email") or "").strip()
        if raw:
            try:
                validate_email(raw)
            except forms.ValidationError:
                raise forms.ValidationError("Enter a valid email address.")
        return raw
