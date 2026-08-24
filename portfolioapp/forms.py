from django import forms

from .models import Appointment, InspectionRequest


class InspectionRequestForm(forms.ModelForm):
    class Meta:
        model = InspectionRequest
        fields = [
            "homeowner_names",
            "phone_number",
            "email",
            "property_address",
            "known_issues",
            "notification_preference",
        ]
        widgets = {
            "homeowner_names": forms.TextInput(
                attrs={"placeholder": "e.g. John & Jane Smith"}
            ),
            "phone_number": forms.TextInput(attrs={"placeholder": "(414) 123-4567"}),
            "email": forms.EmailInput(attrs={"placeholder": "you@example.com"}),
            "property_address": forms.TextInput(
                attrs={"placeholder": "123 Main St, City, WI"}
            ),
            "known_issues": forms.Textarea(
                attrs={
                    "placeholder": "Additional Information",
                    "rows": 3,
                }
            ),
            "notification_preference": forms.RadioSelect,
        }
        labels = {
            "homeowner_names": "Homeowner Name/s",
            "phone_number": "Phone Number",
            "email": "Email",
            "property_address": "Property Address",
            "known_issues": "Additional Information (Optional)",
            "notification_preference": "How should we send your confirmation and reminders?",
        }

    def clean_phone_number(self):
        phone = self.cleaned_data["phone_number"].strip()
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            raise forms.ValidationError("Enter a valid phone number.")
        return phone


class BookingLookupForm(forms.Form):
    """
    The "Manage My Appointment" gate - just the booking number, since
    that's the same unguessable credential the confirmation/email link
    already relies on.
    """

    booking_number = forms.CharField(
        label="Booking Confirmation Number",
        widget=forms.TextInput(attrs={"placeholder": "IRI-XXXX-XXXX"}),
    )

    def clean_booking_number(self):
        raw = self.cleaned_data["booking_number"]
        appointment = Appointment.find_by_booking_number(raw)
        if appointment is None:
            raise forms.ValidationError(
                "We couldn't find an appointment with that confirmation "
                "number. Double check it and try again."
            )
        self.cleaned_data["appointment"] = appointment
        return raw
