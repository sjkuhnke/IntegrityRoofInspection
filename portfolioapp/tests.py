import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import availability
from .models import Appointment, InspectionRequest

BOOKING_NUMBER_RE = re.compile(r"^IRI-[A-Z2-9]{4}-[A-Z2-9]{4}$")

VALID_INTAKE_DATA = {
    "homeowner_names": "John & Jane Smith",
    "phone_number": "(262) 555-0142",
    "email": "jsmith@example.com",
    "property_address": "123 Main St, South Milwaukee, WI",
    "roof_age": "12 years",
    "known_issues": "A few missing shingles near the chimney",
}


def next_available_slot(start_offset=1, search_days=30):
    """
    Walk forward from today until we find a real (date, time) that
    `availability.offered_time_slots` actually offers, so tests aren't
    hard-coded against the placeholder business-hours pattern.
    """
    today = timezone.localdate()
    for offset in range(start_offset, start_offset + search_days):
        candidate_date = today + timedelta(days=offset)
        offered = availability.offered_time_slots(candidate_date)
        if offered:
            # Sort for a deterministic pick across test runs.
            return candidate_date, sorted(offered)[0]
    raise AssertionError("Could not find an offered slot in the search window.")


class IntakeGateTests(TestCase):
    def test_schedule_page_redirects_without_intake(self):
        response = self.client.get(reverse("schedule"))
        self.assertRedirects(
            response,
            f"{reverse('schedule_intake')}?needs_info=1",
            fetch_redirect_response=False,
        )

    def test_valid_intake_creates_request_and_unlocks_schedule(self):
        response = self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)

        self.assertRedirects(response, reverse("schedule"))
        self.assertEqual(InspectionRequest.objects.count(), 1)

        saved = InspectionRequest.objects.first()
        self.assertEqual(saved.email, VALID_INTAKE_DATA["email"])

        # Now that the session has a pointer to the saved request, the
        # calendar should render instead of redirecting.
        schedule_response = self.client.get(reverse("schedule"))
        self.assertEqual(schedule_response.status_code, 200)

    def test_invalid_intake_shows_errors_and_does_not_create_a_row(self):
        bad_data = dict(VALID_INTAKE_DATA, email="not-an-email")
        response = self.client.post(reverse("schedule_intake"), bad_data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(InspectionRequest.objects.exists())
        self.assertFormError(response.context["form"], "email", "Enter a valid email address.")

    def test_intake_data_survives_a_session_pointer_loss(self):
        """
        If the session forgets which InspectionRequest belongs to this
        visitor (cleared cookies, new session, etc.), the calendar route
        sends them back to the gate rather than 500ing or leaking
        someone else's info - but the original data they entered is
        still safely in the database.
        """
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.assertEqual(InspectionRequest.objects.count(), 1)

        self.client.session.flush()

        response = self.client.get(reverse("schedule"))
        self.assertRedirects(
            response,
            f"{reverse('schedule_intake')}?needs_info=1",
            fetch_redirect_response=False,
        )
        # The row itself is untouched.
        self.assertEqual(InspectionRequest.objects.count(), 1)


class AvailabilityApiTests(TestCase):
    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)

    def test_requires_month_param(self):
        response = self.client.get(reverse("schedule_availability"))
        self.assertEqual(response.status_code, 400)

    def test_returns_available_slots_for_month(self):
        target_date, target_time = next_available_slot()
        month_str = target_date.strftime("%Y-%m")

        response = self.client.get(
            reverse("schedule_availability"), {"month": month_str}
        )
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        day_slots = payload["available"].get(target_date.isoformat(), [])
        self.assertIn(target_time, day_slots)

    def test_booked_slot_disappears_from_availability(self):
        target_date, target_time = next_available_slot()

        inspection_request = InspectionRequest.objects.first()
        Appointment.objects.create(
            inspection_request=inspection_request,
            date=target_date,
            time_label=target_time,
        )

        response = self.client.get(
            reverse("schedule_availability"), {"month": target_date.strftime("%Y-%m")}
        )
        day_slots = response.json()["available"].get(target_date.isoformat(), [])
        self.assertNotIn(target_time, day_slots)


class BookingFlowTests(TestCase):
    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.inspection_request = InspectionRequest.objects.first()

    def _post_booking(self, date, time):
        return self.client.post(
            reverse("schedule_confirmation"),
            {"date": date.isoformat(), "time": time, "confirmed": "on"},
        )

    def test_booking_requires_intake_session(self):
        self.client.session.flush()
        target_date, target_time = next_available_slot()

        response = self._post_booking(target_date, target_time)
        self.assertRedirects(
            response,
            f"{reverse('schedule_intake')}?needs_info=1",
            fetch_redirect_response=False,
        )
        self.assertFalse(Appointment.objects.exists())

    def test_successful_booking_redirects_to_confirmation_by_booking_number(self):
        target_date, target_time = next_available_slot()

        response = self._post_booking(target_date, target_time)

        self.assertEqual(Appointment.objects.count(), 1)
        appointment = Appointment.objects.first()

        self.assertRedirects(
            response,
            reverse("schedule_confirmed", args=[appointment.booking_number]),
        )
        self.assertEqual(appointment.date, target_date)
        self.assertEqual(appointment.time_label, target_time)
        self.assertEqual(appointment.inspection_request, self.inspection_request)

    def test_booking_number_format_is_unguessable_pattern(self):
        target_date, target_time = next_available_slot()
        self._post_booking(target_date, target_time)

        appointment = Appointment.objects.first()
        self.assertRegex(appointment.booking_number, BOOKING_NUMBER_RE)

    def test_missing_confirmation_checkbox_is_rejected(self):
        target_date, target_time = next_available_slot()

        response = self.client.post(
            reverse("schedule_confirmation"),
            {"date": target_date.isoformat(), "time": target_time},
        )

        self.assertRedirects(response, reverse("schedule"))
        self.assertFalse(Appointment.objects.exists())

    def test_cannot_book_a_time_the_business_does_not_offer(self):
        target_date, _ = next_available_slot()
        offered = availability.offered_time_slots(target_date)
        not_offered = next(t for t in availability.TIME_SLOTS if t not in offered)

        response = self._post_booking(target_date, not_offered)

        self.assertRedirects(response, reverse("schedule"))
        self.assertFalse(Appointment.objects.exists())

    def test_double_booking_the_same_slot_is_rejected(self):
        target_date, target_time = next_available_slot()

        self._post_booking(target_date, target_time)
        self.assertEqual(Appointment.objects.count(), 1)

        # A second visitor (fresh session) fills out intake and tries to
        # grab the exact same slot.
        self.client.session.flush()
        self.client.post(
            reverse("schedule_intake"),
            dict(VALID_INTAKE_DATA, email="second@example.com"),
        )

        second_response = self._post_booking(target_date, target_time)

        # Rejected: still only one scheduled appointment for that slot.
        self.assertRedirects(second_response, reverse("schedule"))
        self.assertEqual(
            Appointment.objects.filter(
                date=target_date, time_label=target_time, status="scheduled"
            ).count(),
            1,
        )

    def test_database_constraint_itself_prevents_duplicate_active_slots(self):
        """
        Belt-and-suspenders: even bypassing the view entirely, the DB
        constraint refuses a second scheduled Appointment for the same
        (date, time).
        """
        target_date, target_time = next_available_slot()

        Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=target_date,
            time_label=target_time,
        )

        other_request = InspectionRequest.objects.create(
            homeowner_names="Second Homeowner",
            phone_number="555-0100",
            email="other@example.com",
            property_address="456 Other St",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Appointment.objects.create(
                    inspection_request=other_request,
                    date=target_date,
                    time_label=target_time,
                )

    def test_cancelling_an_appointment_frees_the_slot(self):
        target_date, target_time = next_available_slot()

        appointment = Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=target_date,
            time_label=target_time,
        )
        appointment.status = Appointment.STATUS_CANCELLED
        appointment.save()

        # Should not raise - the partial unique constraint only guards
        # "scheduled" rows.
        rebooked = Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=target_date,
            time_label=target_time,
        )
        self.assertNotEqual(rebooked.booking_number, appointment.booking_number)


class ConfirmationPageTests(TestCase):
    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.inspection_request = InspectionRequest.objects.first()
        self.target_date, self.target_time = next_available_slot()
        self.appointment = Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=self.target_date,
            time_label=self.target_time,
        )

    def test_confirmation_page_shows_booking_details(self):
        response = self.client.get(
            reverse("schedule_confirmed", args=[self.appointment.booking_number])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.appointment.booking_number)
        self.assertContains(response, self.target_time)

    def test_refreshing_confirmation_page_does_not_duplicate_or_error(self):
        """
        The confirmation URL is keyed on the booking number (PRG
        pattern), so hitting refresh is just a GET - it can never
        resubmit the booking form or create a second Appointment.
        """
        url = reverse("schedule_confirmed", args=[self.appointment.booking_number])

        first = self.client.get(url)
        second = self.client.get(url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_unknown_booking_number_404s(self):
        response = self.client.get(
            reverse("schedule_confirmed", args=["IRI-ZZZZ-ZZZZ"])
        )
        self.assertEqual(response.status_code, 404)


class AvailabilityRulesTests(TestCase):
    """Unit tests for the pure business-rule module, independent of Django views."""

    def test_offered_slots_never_include_8pm_on_a_regular_weekday(self):
        # Pick a weekday that isn't divisible by 5 or 7 to hit the
        # "normal weekday" branch.
        today = timezone.localdate()
        for offset in range(30):
            candidate = today + timedelta(days=offset)
            if candidate.weekday() < 5 and candidate.day % 5 and candidate.day % 7:
                self.assertNotIn("8:00 PM", availability.offered_time_slots(candidate))
                return
        self.fail("Could not find a plain weekday in the search window.")

    def test_weekends_only_offer_afternoon_evening_slots(self):
        today = timezone.localdate()
        for offset in range(30):
            candidate = today + timedelta(days=offset)
            if candidate.weekday() in (5, 6):
                self.assertEqual(
                    availability.offered_time_slots(candidate),
                    {"4:00 PM", "6:00 PM"},
                )
                return
        self.fail("Could not find a weekend day in the search window.")

    def test_dates_far_in_the_future_are_not_bookable(self):
        too_far = timezone.localdate() + timedelta(
            days=availability.BOOKING_WINDOW_DAYS + 1
        )
        self.assertFalse(availability.is_bookable_date(too_far))

    def test_dates_in_the_past_are_not_bookable(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        self.assertFalse(availability.is_bookable_date(yesterday))