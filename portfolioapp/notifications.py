"""
Booking confirmation, reschedule, cancellation, and reminder delivery.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse

logger = logging.getLogger(__name__)

COMPANY_NOTIFICATION_EMAIL = getattr(
    settings, "COMPANY_NOTIFICATION_EMAIL"
)


def _site_url():
    """
    Absolute base URL used to build links inside emails
    """
    site_url = getattr(settings, "SITE_URL", "")
    if not site_url:
        logger.warning(
            "SITE_URL is not configured - links in outgoing emails will be relative."
        )
    return site_url.rstrip("/")


def manage_booking_url(appointment):
    """Absolute link to the homeowner-facing view/reschedule/cancel page."""
    return f"{_site_url()}{reverse('manage_booking', args=[appointment.booking_number])}"


def schedule_url():
    """Absolute link to start booking a brand-new inspection."""
    return f"{_site_url()}{reverse('schedule_intake')}"


def _send_email(to_email, subject, body, html_body=None):
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[to_email],
            html_message=html_body,
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send email to %s (%r)", to_email, subject)


def _send_text(to_phone, body):
    account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", None)
    auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", None)
    from_number = getattr(settings, "TWILIO_FROM_NUMBER", None)

    if not (account_sid and auth_token and from_number):
        logger.warning(
            "SMS not sent (Twilio not configured). Would have texted %s: %s",
            to_phone,
            body,
        )
        return

    try:
        from twilio.rest import Client  # optional dependency, imported lazily
    except ImportError:
        logger.warning(
            "twilio package not installed; cannot send SMS to %s", to_phone
        )
        return

    try:
        Client(account_sid, auth_token).messages.create(
            to=to_phone, from_=from_number, body=body
        )
    except Exception:
        logger.exception("Failed to send SMS to %s", to_phone)


def _appointment_summary(appointment):
    return (
        f"{appointment.date:%B} {appointment.date.day}, {appointment.date:%Y} "
        f"at {appointment.time_label}"
    )


def _date_time_summary(date, time_label):
    return f"{date:%B} {date.day}, {date:%Y} at {time_label}"


# --------------------------------------------------------------------------
# New booking
# --------------------------------------------------------------------------

def _homeowner_confirmation_text(appointment):
    return (
        f"Thanks for scheduling your free roof inspection with Integrity Roof "
        f"Inspection!\n\n"
        f"Date & time: {_appointment_summary(appointment)}\n"
        f"Booking confirmation number: {appointment.booking_number}\n\n"
        f"Save this number if you need to reference your appointment later. "
        f"Please make sure all homeowners are present at the time of the "
        f"inspection.\n\n"
        f"Manage or cancel your appointment: {manage_booking_url(appointment)}\n\n"
        f"Questions? Call us at (262) 909-6382."
    )


def _homeowner_confirmation_html(appointment):
    return render_to_string(
        "emails/booking_confirmation.html",
        {
            "appointment_summary": _appointment_summary(appointment),
            "booking_number": appointment.booking_number,
            "manage_url": manage_booking_url(appointment),
        },
    )


def _company_notification_message(appointment, extra_note=""):
    request = appointment.inspection_request
    lines = [
        f"When: {_appointment_summary(appointment)}",
        f"Homeowner(s): {request.homeowner_names}",
        f"Phone: {request.phone_number}",
        f"Email: {request.email}",
        f"Address: {request.property_address}",
        f"Approx. roof age: {request.roof_age or 'not provided'}",
        f"Known issues: {request.known_issues or 'none noted'}",
        f"Notification preference: {request.get_notification_preference_display()}",
        f"Manage this appointment: {manage_booking_url(appointment)}",
    ]
    header = f"{extra_note}Booking: {appointment.booking_number}\n\n" if extra_note else (
        f"New inspection booked: {appointment.booking_number}\n\n"
    )
    return header + "\n".join(lines)


def send_new_booking_notifications(appointment):
    """Confirmation to the homeowner (per their preference) + always to the company."""
    request = appointment.inspection_request
    subject = f"Your roof inspection is booked - {appointment.booking_number}"

    if request.wants_email:
        _send_email(
            request.email,
            subject,
            _homeowner_confirmation_text(appointment),
            html_body=_homeowner_confirmation_html(appointment),
        )

    if request.wants_text:
        _send_text(request.phone_number, _homeowner_confirmation_text(appointment))

    _send_email(
        COMPANY_NOTIFICATION_EMAIL,
        subject=f"New inspection booked - {appointment.booking_number}",
        body=_company_notification_message(appointment),
    )


# --------------------------------------------------------------------------
# Reschedule
# --------------------------------------------------------------------------

def _reschedule_text(appointment, previous_summary):
    return (
        f"Your free roof inspection with Integrity Roof Inspection has been "
        f"rescheduled.\n\n"
        f"Previous date & time: {previous_summary}\n"
        f"New date & time: {_appointment_summary(appointment)}\n"
        f"Booking confirmation number: {appointment.booking_number}\n\n"
        f"Please make sure all homeowners are present at the new time.\n\n"
        f"Manage or cancel your appointment: {manage_booking_url(appointment)}\n\n"
        f"Questions? Call us at (262) 909-6382."
    )


def _reschedule_html(appointment, previous_summary):
    return render_to_string(
        "emails/reschedule_confirmation.html",
        {
            "previous_summary": previous_summary,
            "appointment_summary": _appointment_summary(appointment),
            "booking_number": appointment.booking_number,
            "manage_url": manage_booking_url(appointment),
        },
    )


def send_reschedule_notifications(appointment, previous_date_time):
    """
    previous_date_time is a (date, time_label) tuple for the slot the
    appointment *used* to be at, so the message can show old -> new.
    """
    request = appointment.inspection_request
    previous_summary = _date_time_summary(*previous_date_time)
    subject = f"Your roof inspection was rescheduled - {appointment.booking_number}"

    if request.wants_email:
        _send_email(
            request.email,
            subject,
            _reschedule_text(appointment, previous_summary),
            html_body=_reschedule_html(appointment, previous_summary),
        )

    if request.wants_text:
        _send_text(request.phone_number, _reschedule_text(appointment, previous_summary))

    _send_email(
        COMPANY_NOTIFICATION_EMAIL,
        subject=f"Inspection rescheduled - {appointment.booking_number}",
        body=_company_notification_message(
            appointment,
            extra_note=f"Rescheduled from {previous_summary} to {_appointment_summary(appointment)}. ",
        ),
    )


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------

def _cancellation_text(appointment):
    return (
        f"As requested, your free roof inspection with Integrity Roof "
        f"Inspection has been cancelled.\n\n"
        f"Cancelled date & time: {_appointment_summary(appointment)}\n"
        f"Booking confirmation number: {appointment.booking_number}\n\n"
        f"Changed your mind? Schedule a new free inspection any time: "
        f"{schedule_url()}\n\n"
        f"Questions? Call us at (262) 909-6382."
    )


def _cancellation_html(appointment):
    return render_to_string(
        "emails/cancellation_confirmation.html",
        {
            "appointment_summary": _appointment_summary(appointment),
            "booking_number": appointment.booking_number,
            "schedule_url": schedule_url(),
        },
    )


def send_cancellation_notifications(appointment):
    request = appointment.inspection_request
    subject = f"Your roof inspection was cancelled - {appointment.booking_number}"

    if request.wants_email:
        _send_email(
            request.email,
            subject,
            _cancellation_text(appointment),
            html_body=_cancellation_html(appointment),
        )

    if request.wants_text:
        _send_text(request.phone_number, _cancellation_text(appointment))

    _send_email(
        COMPANY_NOTIFICATION_EMAIL,
        subject=f"Inspection cancelled - {appointment.booking_number}",
        body=_company_notification_message(appointment, extra_note="Cancelled. "),
    )


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------

def _reminder_text(appointment, hours_before):
    when = "tomorrow" if hours_before == 24 else "in about an hour"
    return (
        f"Reminder: your free roof inspection with Integrity Roof Inspection "
        f"is {when} - {_appointment_summary(appointment)}.\n\n"
        f"Booking confirmation number: {appointment.booking_number}\n"
        f"Please make sure all homeowners are present.\n\n"
        f"Need to reschedule or cancel? {manage_booking_url(appointment)}"
    )


def _reminder_html(appointment, hours_before):
    when = "tomorrow" if hours_before == 24 else "in about an hour"
    return render_to_string(
        "emails/reminder.html",
        {
            "when": when,
            "appointment_summary": _appointment_summary(appointment),
            "booking_number": appointment.booking_number,
            "manage_url": manage_booking_url(appointment),
        },
    )


def send_reminder(appointment, hours_before):
    """hours_before is 24 or 1 - which reminder copy to use."""
    request = appointment.inspection_request
    subject = f"Reminder: roof inspection {_appointment_summary(appointment)}"

    if request.wants_email:
        _send_email(
            request.email,
            subject,
            _reminder_text(appointment, hours_before),
            html_body=_reminder_html(appointment, hours_before),
        )

    if request.wants_text:
        _send_text(request.phone_number, _reminder_text(appointment, hours_before))
