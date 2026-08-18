import calendar as calendar_module
import logging
from datetime import date as date_cls

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from . import availability, notifications
from .forms import BookingLookupForm, InspectionRequestForm
from .models import Appointment, InspectionRequest, normalize_booking_number

logger = logging.getLogger(__name__)

INSPECTION_REQUEST_SESSION_KEY = "inspection_request_id"


def home(request):
    return render(request, "home.html")


def terms(request):
    return render(request, "terms.html")


def privacy(request):
    return render(request, "privacy.html")


# --------------------------------------------------------------------------
# Step 1: intake gate
# --------------------------------------------------------------------------

def schedule_intake(request):
    """
    Collect homeowner info before letting anyone see the calendar.

    On success we persist the InspectionRequest to the DB immediately
    (not just in the session) and store its id in the session. That way
    a browser refresh, tab close, or session-cookie hiccup on the
    calendar page never loses the info the person already typed in -
    only the *pointer* to it could be lost, in which case we just send
    them back here to fill it out again.

    If the session already points at an InspectionRequest (they've been
    here before this visit), the form is bound to that same row instead
    of starting blank - so using the "back"/"edit info" link updates
    their existing info rather than creating an orphaned duplicate row
    every time they go back and forth.
    """
    existing_request = _get_active_inspection_request(request)

    if request.method == "POST":
        form = InspectionRequestForm(request.POST, instance=existing_request)

        if form.is_valid():
            inspection_request = form.save()
            request.session[INSPECTION_REQUEST_SESSION_KEY] = inspection_request.pk
            return redirect("schedule")
    else:
        form = InspectionRequestForm(instance=existing_request)

    return render(
        request,
        "schedule_intake.html",
        {"form": form, "is_editing": existing_request is not None},
    )


def schedule_reset(request):
    """
    "Start over" - forget the current InspectionRequest entirely so the
    next visit to the gate is a blank form for a new person/property,
    rather than editing whoever's info happened to be in this session.
    The InspectionRequest row itself is left in the database (it's just
    a lead record at that point) - only the session pointer is cleared.
    """
    request.session.pop(INSPECTION_REQUEST_SESSION_KEY, None)
    request.session.pop("schedule_error", None)
    return redirect("schedule_intake")


def _get_active_inspection_request(request):
    """Fetch the InspectionRequest referenced by the session, if any."""
    inspection_request_id = request.session.get(INSPECTION_REQUEST_SESSION_KEY)

    if not inspection_request_id:
        return None

    return InspectionRequest.objects.filter(pk=inspection_request_id).first()


# --------------------------------------------------------------------------
# Step 2: calendar
# --------------------------------------------------------------------------

def schedule(request):
    inspection_request = _get_active_inspection_request(request)

    if inspection_request is None:
        return redirect(f"{reverse('schedule_intake')}?needs_info=1")

    error_message = request.session.pop("schedule_error", None)

    return render(
        request,
        "schedule.html",
        {
            "inspection_request": inspection_request,
            "error_message": error_message,
            "time_slots": availability.TIME_SLOTS,
        },
    )


def schedule_availability(request):
    """
    JSON API the calendar's JS calls (on load and on month navigation)
    to find out which {date: [times]} are actually bookable.

    This is the same source of truth used to validate the POST in
    schedule_confirmation (and manage_reschedule), so the calendar can
    never show something as available that the server would then reject.

    Accepts an optional ?exclude=<booking number>, used by the "manage
    my appointment" reschedule calendar so an appointment's *own*
    current slot shows as available (pickable/keepable) instead of
    looking taken by itself.
    """
    month_param = request.GET.get("month")  # "YYYY-MM"
    exclude_booking_number = request.GET.get("exclude", "")

    try:
        year, month = (int(part) for part in month_param.split("-"))
        first_of_month = date_cls(year, month, 1)
    except (AttributeError, ValueError):
        return JsonResponse({"error": "Invalid or missing ?month=YYYY-MM"}, status=400)

    days_in_month = calendar_module.monthrange(year, month)[1]

    booked_by_date = _booked_labels_for_month(
        year, month, exclude_booking_number=exclude_booking_number
    )

    available_by_date = {}
    for day in range(1, days_in_month + 1):
        current = first_of_month.replace(day=day)
        slots = availability.available_time_slots(
            current, booked_by_date.get(current, set())
        )
        if slots:
            # Keep a stable, human-friendly order rather than set order.
            available_by_date[current.isoformat()] = [
                t for t in availability.TIME_SLOTS if t in slots
            ]

    return JsonResponse({"available": available_by_date})


def _booked_labels_for_month(year, month, exclude_booking_number=""):
    """{date: {time_label, ...}} for every currently-scheduled appointment in the month."""
    qs = Appointment.objects.filter(
        status=Appointment.STATUS_SCHEDULED,
        date__year=year,
        date__month=month,
    )

    if exclude_booking_number:
        qs = qs.exclude(
            booking_number=normalize_booking_number(exclude_booking_number)
        )

    booked = {}
    for appt_date, time_label in qs.values_list("date", "time_label"):
        booked.setdefault(appt_date, set()).add(time_label)

    return booked


# --------------------------------------------------------------------------
# Step 3: booking (POST) + confirmation (GET, PRG pattern)
# --------------------------------------------------------------------------

@require_http_methods(["POST"])
def schedule_confirmation(request):
    """
    Create the Appointment.

    Concurrency: two people can both load the calendar and see the same
    slot as open, then both submit around the same instant. We don't try
    to prevent that at the application level with locks - instead we let
    the database's partial unique constraint (see Appointment.Meta) be
    the final word. Whoever's INSERT lands second gets an IntegrityError,
    which we catch and bounce back to the calendar with a plain-language
    error instead of a 500.

    On success we redirect (Post/Redirect/Get) to a URL keyed on the
    booking number, so refreshing the confirmation page never re-submits
    the form and always shows the same booking.
    """
    inspection_request = _get_active_inspection_request(request)

    if inspection_request is None:
        return redirect(f"{reverse('schedule_intake')}?needs_info=1")

    selected_date_raw = request.POST.get("date", "")
    selected_time = request.POST.get("time", "")
    confirmed_checkbox = request.POST.get("confirmed")

    error_message = None

    try:
        selected_date = date_cls.fromisoformat(selected_date_raw)
    except ValueError:
        selected_date = None

    if not confirmed_checkbox:
        error_message = "Please confirm the date and time before submitting."
    elif selected_date is None or not selected_time:
        error_message = "Please select a date and time."
    elif not availability.is_bookable_date(selected_date):
        error_message = "That date is no longer available. Please choose another."
    else:
        booked_labels = _booked_labels_for_month(
            selected_date.year, selected_date.month
        ).get(selected_date, set())

        if not availability.is_slot_available(
            selected_date, selected_time, booked_labels
        ):
            error_message = (
                "That time was just booked by someone else. "
                "Please pick another available time."
            )

    if error_message:
        request.session["schedule_error"] = error_message
        return redirect("schedule")

    try:
        with transaction.atomic():
            appointment = Appointment.objects.create(
                inspection_request=inspection_request,
                date=selected_date,
                time_label=selected_time,
            )
    except IntegrityError:
        # Someone else's booking for this exact slot committed first.
        request.session["schedule_error"] = (
            "That time was just booked by someone else. "
            "Please pick another available time."
        )
        return redirect("schedule")

    try:
        notifications.send_new_booking_notifications(appointment)
    except Exception:
        # The booking itself already succeeded and is safely in the DB -
        # a notification hiccup shouldn't turn a successful booking into
        # an error page for the homeowner.
        logger.exception(
            "Failed to send booking notifications for %s", appointment.booking_number
        )

    return redirect("schedule_confirmed", booking_number=appointment.booking_number)


def schedule_confirmed(request, booking_number):
    appointment = get_object_or_404(
        Appointment.objects.select_related("inspection_request"),
        booking_number=booking_number,
    )

    return render(
        request,
        "schedule_confirm.html",
        {
            "appointment": appointment,
            "inspection_date": f"{appointment.date:%B} {appointment.date.day}, {appointment.date:%Y}",
            "inspection_time": appointment.time_label,
            "booking_number": appointment.booking_number,
        },
    )


# --------------------------------------------------------------------------
# Manage an existing appointment: look up by booking number, then
# view/reschedule/cancel it. This deliberately uses the same "booking
# number is the credential" model as schedule_confirmed above - there's
# no login, the unguessable booking number *is* the proof someone is
# allowed to see/change this appointment (it's emailed/texted only to
# the homeowner and printed on their confirmation page).
# --------------------------------------------------------------------------

def manage_lookup(request):
    """Gate page: homeowner types in their booking number to find their appointment."""
    if request.method == "POST":
        form = BookingLookupForm(request.POST)

        if form.is_valid():
            appointment = form.cleaned_data["appointment"]
            return redirect("manage_booking", booking_number=appointment.booking_number)
    else:
        form = BookingLookupForm()

    return render(request, "schedule_manage_lookup.html", {"form": form})


def _get_appointment_or_404(booking_number):
    return get_object_or_404(
        Appointment.objects.select_related("inspection_request"),
        booking_number=normalize_booking_number(booking_number),
    )


def manage_booking(request, booking_number):
    """
    Shows the appointment's current details plus (if it's still active)
    a reschedule calendar and a cancel option. Cancelled appointments
    are shown read-only rather than 404ing, so a homeowner clicking an
    old email link gets a clear "this was cancelled" instead of a dead
    page.
    """
    appointment = _get_appointment_or_404(booking_number)

    manage_error = request.session.pop("manage_error", None)
    manage_success = request.session.pop("manage_success", None)

    return render(
        request,
        "schedule_manage.html",
        {
            "appointment": appointment,
            "manage_error": manage_error,
            "manage_success": manage_success,
            "time_slots": availability.TIME_SLOTS,
        },
    )


@require_http_methods(["POST"])
def manage_reschedule(request, booking_number):
    """
    Move an existing, still-scheduled Appointment to a new date/time.

    This updates the existing row in place (rather than cancelling +
    creating a new one) so the booking number, history, and any prior
    reminder tracking stay attached to one appointment. The 24h/1h
    reminder flags are reset, since a reminder already sent for the old
    time doesn't mean anything about the new one.
    """
    appointment = _get_appointment_or_404(booking_number)

    if appointment.is_cancelled:
        request.session["manage_error"] = (
            "This appointment has already been cancelled and can't be "
            "rescheduled. Please schedule a new inspection instead."
        )
        return redirect("manage_booking", booking_number=appointment.booking_number)

    selected_date_raw = request.POST.get("date", "")
    selected_time = request.POST.get("time", "")
    confirmed_checkbox = request.POST.get("confirmed")

    error_message = None

    try:
        selected_date = date_cls.fromisoformat(selected_date_raw)
    except ValueError:
        selected_date = None

    if not confirmed_checkbox:
        error_message = "Please confirm the new date and time before submitting."
    elif selected_date is None or not selected_time:
        error_message = "Please select a date and time."
    elif (
        selected_date == appointment.date
        and selected_time == appointment.time_label
    ):
        error_message = (
            "That's already your scheduled date and time - nothing was changed."
        )
    elif not availability.is_bookable_date(selected_date):
        error_message = "That date is no longer available. Please choose another."
    else:
        booked_labels = _booked_labels_for_month(
            selected_date.year,
            selected_date.month,
            exclude_booking_number=appointment.booking_number,
        ).get(selected_date, set())

        if not availability.is_slot_available(
            selected_date, selected_time, booked_labels
        ):
            error_message = (
                "That time was just booked by someone else. "
                "Please pick another available time."
            )

    if error_message:
        request.session["manage_error"] = error_message
        return redirect("manage_booking", booking_number=appointment.booking_number)

    previous_summary = (appointment.date, appointment.time_label)

    try:
        with transaction.atomic():
            appointment.date = selected_date
            appointment.time_label = selected_time
            # A reminder already sent for the old slot doesn't apply to
            # the new one
            appointment.reminder_24h_sent_at = None
            appointment.reminder_1h_sent_at = None
            appointment.save(
                update_fields=[
                    "date",
                    "time_label",
                    "reminder_24h_sent_at",
                    "reminder_1h_sent_at",
                    "updated_at",
                ]
            )
    except IntegrityError:
        # Someone else's booking for this exact slot committed first.
        request.session["manage_error"] = (
            "That time was just booked by someone else. "
            "Please pick another available time."
        )
        return redirect("manage_booking", booking_number=appointment.booking_number)

    try:
        notifications.send_reschedule_notifications(appointment, previous_summary)
    except Exception:
        logger.exception(
            "Failed to send reschedule notifications for %s", appointment.booking_number
        )

    request.session["manage_success"] = "Your appointment has been rescheduled."
    return redirect("manage_booking", booking_number=appointment.booking_number)


@require_http_methods(["POST"])
def manage_cancel(request, booking_number):
    """Cancel an appointment. Freeing the slot is automatic (see Appointment.Meta)."""
    appointment = _get_appointment_or_404(booking_number)

    if appointment.is_cancelled:
        request.session["manage_error"] = "This appointment is already cancelled."
        return redirect("manage_booking", booking_number=appointment.booking_number)

    appointment.status = Appointment.STATUS_CANCELLED
    appointment.save(update_fields=["status", "updated_at"])

    try:
        notifications.send_cancellation_notifications(appointment)
    except Exception:
        logger.exception(
            "Failed to send cancellation notifications for %s", appointment.booking_number
        )

    request.session["manage_success"] = "Your appointment has been cancelled."
    return redirect("manage_booking", booking_number=appointment.booking_number)