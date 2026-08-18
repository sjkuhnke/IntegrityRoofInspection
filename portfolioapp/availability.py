from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


TIME_SLOTS = [
    "10:00 AM",
    "12:00 PM",
    "2:00 PM",
    "4:00 PM",
    "6:00 PM",
    "8:00 PM",
]

BUSINESS_TIMEZONE = ZoneInfo("America/Chicago")

# How far into the future homeowners are allowed to browse/book.
BOOKING_WINDOW_DAYS = 90

# Slots starting within this many minutes from now (Central Time) are
# treated as unavailable today - not enough notice for a tech to make it.
BOOKING_LEAD_TIME_MINUTES = 60


def _now():
    return datetime.now(BUSINESS_TIMEZONE)


def _today():
    return _now().date()


def is_bookable_date(date):
    """Reject dates in the past or too far out, regardless of time-slot rules."""
    today = _today()
    return today <= date <= today + timedelta(days=BOOKING_WINDOW_DAYS)


def _parse_time_label(time_label):
    """"2:00 PM" -> datetime.time(14, 0)"""
    return datetime.strptime(time_label, "%I:%M %p").time()


def slot_datetime(date, time_label):
    """Timezone-aware Central-Time datetime for when a given slot starts."""
    return datetime.combine(date, _parse_time_label(time_label), tzinfo=BUSINESS_TIMEZONE)


def _is_within_lead_time(date, time_label):
    """
    True if `time_label` on `date` starts less than BOOKING_LEAD_TIME_MINUTES
    from now, or has already started/passed. Always evaluated in Central Time.
    """
    slot_start = slot_datetime(date, time_label)
    cutoff = _now() + timedelta(minutes=BOOKING_LEAD_TIME_MINUTES)
    return slot_start < cutoff


def available_time_slots(date, booked_labels):
    """Offered slots minus whatever's already booked, minus anything too soon."""
    if not is_bookable_date(date):
        return set()

    offered = set(TIME_SLOTS) - set(booked_labels)

    if date == _today():
        offered = {t for t in offered if not _is_within_lead_time(date, t)}

    return offered


def is_slot_available(date, time_label, booked_labels):
    return time_label in available_time_slots(date, booked_labels)
