import calendar as calendar_module
import json
from datetime import date as date_cls

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from . import availability
from .forms import InspectionRequestForm
from .models import Appointment, InspectionRequest

INSPECTION_REQUEST_SESSION_KEY = "inspection_request_id"


def home(request):
    return render(request, "home.html")


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
    """
    if request.method == "POST":
        form = InspectionRequestForm(request.POST)

        if form.is_valid():
            inspection_request = form.save()
            request.session[INSPECTION_REQUEST_SESSION_KEY] = inspection_request.pk
            return redirect("schedule")
    else:
        form = InspectionRequestForm()

    return render(request, "schedule_intake.html", {"form": form})


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
    schedule_confirmation, so the calendar can never show something as
    available that the server would then reject.
    """
    month_param = request.GET.get("month")  # "YYYY-MM"

    try:
        year, month = (int(part) for part in month_param.split("-"))
        first_of_month = date_cls(year, month, 1)
    except (AttributeError, ValueError):
        return JsonResponse({"error": "Invalid or missing ?month=YYYY-MM"}, status=400)

    days_in_month = calendar_module.monthrange(year, month)[1]

    booked_by_date = _booked_labels_for_month(year, month)

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


def _booked_labels_for_month(year, month):
    """{date: {time_label, ...}} for every currently-scheduled appointment in the month."""
    booked = {}
    qs = Appointment.objects.filter(
        status=Appointment.STATUS_SCHEDULED,
        date__year=year,
        date__month=month,
    ).values_list("date", "time_label")

    for appt_date, time_label in qs:
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
