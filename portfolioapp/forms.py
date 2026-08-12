from django import forms

from .models import InspectionRequest


class InspectionRequestForm(forms.ModelForm):
    class Meta:
        model = InspectionRequest
        fields = [
            "homeowner_names",
            "phone_number",
            "email",
            "property_address",
            "roof_age",
            "known_issues",
        ]
        widgets = {
            "homeowner_names": forms.TextInput(
                attrs={"placeholder": "e.g. John & Jane Smith"}
            ),
            "phone_number": forms.TextInput(attrs={"placeholder": "(262) 555-0142"}),
            "email": forms.EmailInput(attrs={"placeholder": "you@example.com"}),
            "property_address": forms.TextInput(
                attrs={"placeholder": "123 Main St, City, WI"}
            ),
            "roof_age": forms.TextInput(
                attrs={"placeholder": "e.g. 12 years, not sure"}
            ),
            "known_issues": forms.Textarea(
                attrs={
                    "placeholder": "Leaks, missing shingles, sagging, etc. (optional)",
                    "rows": 3,
                }
            ),
        }
        labels = {
            "homeowner_names": "Homeowner Name/s",
            "phone_number": "Phone Number",
            "email": "Email",
            "property_address": "Property Address",
            "roof_age": "Approx. Age of Roof",
            "known_issues": "Known Issues with Roof?",
        }

    def clean_phone_number(self):
        phone = self.cleaned_data["phone_number"].strip()
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            raise forms.ValidationError("Enter a valid phone number.")
        return phone