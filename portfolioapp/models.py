import secrets

from django.db import models


# Characters chosen to avoid visual ambiguity when read aloud or typed
BOOKING_NUMBER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
BOOKING_NUMBER_GROUP_LENGTH = 4
BOOKING_NUMBER_GROUPS = 2
BOOKING_NUMBER_PREFIX = "IRI-"


def _random_booking_number():
    parts = [
        "".join(
            secrets.choice(BOOKING_NUMBER_ALPHABET)
            for _ in range(BOOKING_NUMBER_GROUP_LENGTH)
        )
        for _ in range(BOOKING_NUMBER_GROUPS)
    ]
    return BOOKING_NUMBER_PREFIX + "-".join(parts)


class InspectionRequest(models.Model):
    """
    Homeowner intake info, captured by the gate form *before* a specific
    appointment slot has been chosen. A row is created as soon as the
    intake form is submitted so that the person's info survives a page
    refresh (only the session pointer to this row can be lost, not the
    data itself).
    """

    homeowner_names = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=30)
    email = models.EmailField()
    property_address = models.CharField(max_length=255)
    roof_age = models.CharField(max_length=100, blank=True)
    known_issues = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.homeowner_names} ({self.property_address})"


class Appointment(models.Model):
    """
    A confirmed inspection booking for a specific date + time.

    Double-booking protection: a *partial unique constraint* only applies
    to rows with status="scheduled", so the (date, time) pair is only
    guaranteed unique among active bookings. Cancelling an appointment
    frees that slot back up for someone else without losing history.

    This is enforced by the database itself (not just application code),
    so it holds even under concurrent requests racing for the same slot -
    the second INSERT simply fails with an IntegrityError, which the view
    catches and turns into a friendly "that time was just taken" message.
    """

    STATUS_SCHEDULED = "scheduled"
    STATUS_CANCELLED = "cancelled"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_COMPLETED, "Completed"),
    ]

    booking_number = models.CharField(max_length=20, unique=True, editable=False)

    inspection_request = models.ForeignKey(
        InspectionRequest,
        on_delete=models.CASCADE,
        related_name="appointments",
    )

    date = models.DateField()
    time_label = models.CharField(
        max_length=20,
        help_text='Display label for the slot, e.g. "2:00 PM".',
    )

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_SCHEDULED
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "time_label"]
        constraints = [
            models.UniqueConstraint(
                fields=["date", "time_label"],
                condition=models.Q(status="scheduled"),
                name="unique_scheduled_slot",
            )
        ]

    def __str__(self):
        return f"{self.booking_number} — {self.date} at {self.time_label}"

    def save(self, *args, **kwargs):
        if not self.booking_number:
            self.booking_number = self._generate_unique_booking_number()
        super().save(*args, **kwargs)

    @staticmethod
    def _generate_unique_booking_number(max_attempts=10):
        for _ in range(max_attempts):
            candidate = _random_booking_number()
            if not Appointment.objects.filter(booking_number=candidate).exists():
                return candidate

        raise RuntimeError(
            "Could not generate a unique booking number after "
            f"{max_attempts} attempts."
        )
    