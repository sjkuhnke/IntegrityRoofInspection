from django.utils import timezone

TIME_SLOTS = [
    "10:00 AM",
    "12:00 PM",
    "2:00 PM",
    "4:00 PM",
    "6:00 PM",
    "8:00 PM",
]

# How far into the future homeowners are allowed to browse/book.
BOOKING_WINDOW_DAYS = 90


def is_bookable_date(date):
    """Reject dates in the past or too far out, regardless of time-slot rules."""
    today = timezone.localdate()
    return today <= date <= today + timezone.timedelta(days=BOOKING_WINDOW_DAYS)


def available_time_slots(date, booked_labels):
    """Offered slots minus whatever's already booked for that date."""
    if not is_bookable_date(date):
        return set()
    return set(TIME_SLOTS) - set(booked_labels)


def is_slot_available(date, time_label, booked_labels):
    return time_label in available_time_slots(date, booked_labels)
