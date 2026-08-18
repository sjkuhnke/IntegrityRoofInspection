import re
from datetime import date as date_cls
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from . import availability, notifications
from .models import Appointment, InspectionRequest

BOOKING_NUMBER_RE = re.compile(r"^IRI-[A-Z2-9]{4}-[A-Z2-9]{4}$")

VALID_INTAKE_DATA = {
    "homeowner_names": "John & Jane Smith",
    "phone_number": "(920) 716-2890",
    "email": "jsmith@example.com",
    "property_address": "123 Main St, South Milwaukee, WI",
    "roof_age": "12 years",
    "known_issues": "A few missing shingles near the chimney",
    "notification_preference": InspectionRequest.NOTIFY_EMAIL,
}


def next_available_slot(start_offset=1, search_days=30):
    """
    Walk forward from today until we find a real (date, time) that
    `availability.available_time_slots` actually offers, so tests aren't
    hard-coded against slot/lead-time details that might change later.

    Defaults to starting tomorrow (start_offset=1) specifically so the
    lead-time-cutoff behavior (which only applies to *today*) doesn't
    make this helper flaky depending on what time the test happens to run.
    """
    today = availability._today()
    for offset in range(start_offset, start_offset + search_days):
        candidate_date = today + timedelta(days=offset)
        offered = availability.available_time_slots(candidate_date, booked_labels=set())
        if offered:
            # Sort for a deterministic pick across test runs.
            return candidate_date, sorted(offered)[0]
    raise AssertionError("Could not find an available slot in the search window.")


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


class EditInfoAndResetTests(TestCase):
    """
    Covers the "back/edit info" and "start over" flow: going back to the
    gate should let someone fix a typo without creating an orphaned
    duplicate row, and starting over should give them a clean slate.
    """

    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.original = InspectionRequest.objects.first()

    def test_revisiting_the_gate_prefills_the_existing_info(self):
        response = self.client.get(reverse("schedule_intake"))

        self.assertTrue(response.context["is_editing"])
        self.assertEqual(
            response.context["form"].initial["email"], VALID_INTAKE_DATA["email"]
        )

    def test_resubmitting_the_gate_updates_the_same_row_not_a_new_one(self):
        updated_data = dict(VALID_INTAKE_DATA, email="updated@example.com")

        self.client.post(reverse("schedule_intake"), updated_data)

        self.assertEqual(InspectionRequest.objects.count(), 1)
        self.original.refresh_from_db()
        self.assertEqual(self.original.email, "updated@example.com")

    def test_editing_keeps_the_session_pointed_at_the_same_row(self):
        self.client.post(
            reverse("schedule_intake"), dict(VALID_INTAKE_DATA, email="new@example.com")
        )

        self.assertEqual(
            self.client.session["inspection_request_id"], self.original.pk
        )

    def test_start_over_clears_the_session_but_keeps_the_old_row(self):
        response = self.client.get(reverse("schedule_reset"))

        self.assertRedirects(response, reverse("schedule_intake"))
        self.assertNotIn("inspection_request_id", self.client.session)
        # The lead record itself isn't deleted, just forgotten by this session.
        self.assertEqual(InspectionRequest.objects.count(), 1)

    def test_gate_is_blank_again_after_starting_over(self):
        self.client.get(reverse("schedule_reset"))

        response = self.client.get(reverse("schedule_intake"))

        self.assertFalse(response.context["is_editing"])
        self.assertEqual(response.context["form"].initial.get("email"), None)

    def test_a_new_submission_after_starting_over_creates_a_second_row(self):
        self.client.get(reverse("schedule_reset"))

        self.client.post(
            reverse("schedule_intake"),
            dict(VALID_INTAKE_DATA, email="second-person@example.com"),
        )

        self.assertEqual(InspectionRequest.objects.count(), 2)


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

    def test_cannot_book_a_time_that_is_not_a_real_slot(self):
        target_date, _ = next_available_slot()

        response = self._post_booking(target_date, "3:15 PM")

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

    def _freeze_now(self, fixed_datetime):
        """Patch availability._now() so lead-time/timezone logic is deterministic."""
        patcher = mock.patch(
            "portfolioapp.availability._now", return_value=fixed_datetime
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_slot_inside_the_lead_time_window_is_unavailable_today(self):
        today = date_cls(2026, 6, 15)
        # 9:30 AM Central - "10:00 AM" is only 30 minutes out, inside the
        # 60-minute lead time, so it should be filtered.
        self._freeze_now(datetime(2026, 6, 15, 9, 30, tzinfo=availability.BUSINESS_TIMEZONE))

        self.assertNotIn(
            "10:00 AM", availability.available_time_slots(today, booked_labels=set())
        )

    def test_slot_outside_the_lead_time_window_is_available_today(self):
        today = date_cls(2026, 6, 15)
        # 8:00 AM Central - "10:00 AM" is 2 hours out, well past the
        # 60-minute cutoff.
        self._freeze_now(datetime(2026, 6, 15, 8, 0, tzinfo=availability.BUSINESS_TIMEZONE))

        self.assertIn(
            "10:00 AM", availability.available_time_slots(today, booked_labels=set())
        )

    def test_already_past_slot_is_unavailable_today(self):
        today = date_cls(2026, 6, 15)
        self._freeze_now(datetime(2026, 6, 15, 11, 0, tzinfo=availability.BUSINESS_TIMEZONE))

        self.assertNotIn(
            "10:00 AM", availability.available_time_slots(today, booked_labels=set())
        )

    def test_lead_time_cutoff_does_not_affect_future_dates(self):
        tomorrow = date_cls(2026, 6, 16)
        # Even at 11 PM the night before, tomorrow's early slots are fine -
        # the cutoff only ever trims *today*.
        self._freeze_now(datetime(2026, 6, 15, 23, 0, tzinfo=availability.BUSINESS_TIMEZONE))

        self.assertIn(
            "10:00 AM", availability.available_time_slots(tomorrow, booked_labels=set())
        )

    def test_today_is_computed_in_central_time_not_utc(self):
        # 2:00 AM UTC on Jan 1 is 8:00 PM Central on Dec 31 - "today" for
        # the business must be Dec 31, not Jan 1.
        self._freeze_now(
            datetime(2026, 1, 1, 2, 0, tzinfo=ZoneInfo("UTC")).astimezone(
                availability.BUSINESS_TIMEZONE
            )
        )

        self.assertEqual(availability._today(), date_cls(2025, 12, 31))

    def test_dates_far_in_the_future_are_not_bookable(self):
        too_far = availability._today() + timedelta(
            days=availability.BOOKING_WINDOW_DAYS + 1
        )
        self.assertFalse(availability.is_bookable_date(too_far))

    def test_dates_in_the_past_are_not_bookable(self):
        yesterday = availability._today() - timedelta(days=1)
        self.assertFalse(availability.is_bookable_date(yesterday))


class NotificationTests(TestCase):
    """
    Django's test runner automatically swaps EMAIL_BACKEND to the locmem
    backend, so `mail.outbox` collects everything "sent" during a test
    without touching a real mail server.
    """

    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.inspection_request = InspectionRequest.objects.first()
        self.target_date, self.target_time = next_available_slot()

    def _book(self):
        return self.client.post(
            reverse("schedule_confirmation"),
            {
                "date": self.target_date.isoformat(),
                "time": self.target_time,
                "confirmed": "on",
            },
        )

    def test_booking_emails_both_homeowner_and_company_by_default(self):
        self._book()
        appointment = Appointment.objects.first()

        self.assertEqual(len(mail.outbox), 2)

        homeowner_email = next(
            m for m in mail.outbox if m.to == [self.inspection_request.email]
        )
        company_email = next(
            m for m in mail.outbox if m.to == [notifications.COMPANY_NOTIFICATION_EMAIL]
        )

        self.assertIn(appointment.booking_number, homeowner_email.body)
        self.assertIn(self.inspection_request.homeowner_names, company_email.body)

    def test_text_only_preference_skips_the_homeowner_email(self):
        self.client.post(
            reverse("schedule_intake"),
            dict(
                VALID_INTAKE_DATA,
                notification_preference=InspectionRequest.NOTIFY_TEXT,
            ),
        )

        self._book()

        recipients = [m.to for m in mail.outbox]
        self.assertNotIn([self.inspection_request.email], recipients)
        # The company notification isn't tied to the homeowner's
        # preference - it always goes out by email.
        self.assertIn([notifications.COMPANY_NOTIFICATION_EMAIL], recipients)

    def test_unconfigured_sms_logs_a_warning_instead_of_sending(self):
        with self.assertLogs("portfolioapp.notifications", level="WARNING") as captured:
            notifications._send_text("+15555550123", "test message")

        self.assertTrue(any("not sent" in line for line in captured.output))

    def test_a_broken_mail_server_does_not_break_the_booking(self):
        with mock.patch(
            "portfolioapp.notifications.send_mail",
            side_effect=RuntimeError("smtp is down"),
        ):
            with self.assertLogs("portfolioapp.notifications", level="ERROR"):
                response = self._book()

        # The booking still went through even though "sending" blew up -
        # notification delivery is best-effort and never allowed to turn
        # a successful booking into an error for the homeowner.
        self.assertEqual(Appointment.objects.count(), 1)
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.url, reverse("schedule"))


class ReminderCommandTests(TestCase):
    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.inspection_request = InspectionRequest.objects.first()

    def _freeze_django_now(self, central_datetime):
        """Freeze both the command's clock and re-derive the equivalent UTC instant."""
        utc_instant = central_datetime.astimezone(ZoneInfo("UTC"))
        patcher = mock.patch("django.utils.timezone.now", return_value=utc_instant)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _appointment(self, date, time_label, **kwargs):
        return Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=date,
            time_label=time_label,
            **kwargs,
        )

    def test_sends_24h_reminder_when_appointment_is_within_the_window(self):
        now = datetime(2026, 6, 15, 11, 0, tzinfo=availability.BUSINESS_TIMEZONE)
        self._freeze_django_now(now)
        # 23 hours out - inside the 24h window, nowhere near the 1h window.
        appointment = self._appointment(date_cls(2026, 6, 16), "10:00 AM")

        call_command("reminders")

        appointment.refresh_from_db()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(appointment.booking_number, mail.outbox[0].body)
        self.assertIsNotNone(appointment.reminder_24h_sent_at)
        self.assertIsNone(appointment.reminder_1h_sent_at)

    def test_appointment_more_than_24h_out_gets_no_reminder_yet(self):
        now = datetime(2026, 6, 15, 10, 0, tzinfo=availability.BUSINESS_TIMEZONE)
        self._freeze_django_now(now)
        appointment = self._appointment(date_cls(2026, 6, 16), "12:00 PM")  # 26h out

        call_command("reminders")

        appointment.refresh_from_db()
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(appointment.reminder_24h_sent_at)

    def test_reminder_24h_is_not_sent_twice(self):
        now = datetime(2026, 6, 15, 10, 0, tzinfo=availability.BUSINESS_TIMEZONE)
        self._freeze_django_now(now)
        appointment = self._appointment(
            date_cls(2026, 6, 16),
            "10:00 AM",
            reminder_24h_sent_at=now - timedelta(hours=1),
        )

        call_command("reminders")

        # Already marked sent before this run - the command should have
        # left it alone rather than emailing again.
        self.assertEqual(len(mail.outbox), 0)

    def test_1h_reminder_fires_independently_of_24h(self):
        now = datetime(2026, 6, 15, 11, 30, tzinfo=availability.BUSINESS_TIMEZONE)
        self._freeze_django_now(now)
        # 12:00 PM is 30 minutes out - inside the 1h window. Pretend the
        # 24h reminder already went out earlier.
        appointment = self._appointment(
            date_cls(2026, 6, 15),
            "12:00 PM",
            reminder_24h_sent_at=now - timedelta(hours=23),
        )

        call_command("reminders")

        appointment.refresh_from_db()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIsNotNone(appointment.reminder_1h_sent_at)
        # Unchanged - it was already set, so the 24h branch had nothing to do.
        self.assertEqual(
            appointment.reminder_24h_sent_at, now - timedelta(hours=23)
        )

    def test_already_passed_appointments_get_no_reminder(self):
        now = datetime(2026, 6, 15, 12, 30, tzinfo=availability.BUSINESS_TIMEZONE)
        self._freeze_django_now(now)
        appointment = self._appointment(date_cls(2026, 6, 15), "12:00 PM")  # 30 min ago

        call_command("reminders")

        appointment.refresh_from_db()
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(appointment.reminder_24h_sent_at)
        self.assertIsNone(appointment.reminder_1h_sent_at)

    def test_running_the_command_twice_does_not_double_send(self):
        now = datetime(2026, 6, 15, 11, 30, tzinfo=availability.BUSINESS_TIMEZONE)
        self._freeze_django_now(now)
        self._appointment(date_cls(2026, 6, 15), "12:00 PM")

        call_command("reminders")
        first_count = len(mail.outbox)
        call_command("reminders")
        second_count = len(mail.outbox)

        self.assertEqual(first_count, second_count)


class ManageLookupTests(TestCase):
    """The "Manage My Appointment" gate: find an appointment by booking number."""

    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.inspection_request = InspectionRequest.objects.first()
        target_date, target_time = next_available_slot()
        self.appointment = Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=target_date,
            time_label=target_time,
        )

    def test_valid_booking_number_redirects_to_manage_page(self):
        response = self.client.post(
            reverse("manage_lookup"), {"booking_number": self.appointment.booking_number}
        )
        self.assertRedirects(
            response, reverse("manage_booking", args=[self.appointment.booking_number])
        )

    def test_lowercase_and_stray_whitespace_still_find_the_appointment(self):
        messy = self.appointment.booking_number.lower().replace("-", " - ")
        response = self.client.post(reverse("manage_lookup"), {"booking_number": messy})
        self.assertRedirects(
            response, reverse("manage_booking", args=[self.appointment.booking_number])
        )

    def test_unknown_booking_number_shows_a_friendly_error_not_a_500(self):
        response = self.client.post(
            reverse("manage_lookup"), {"booking_number": "IRI-ZZZZ-ZZZZ"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "couldn")

    def test_blank_submission_is_rejected(self):
        response = self.client.post(reverse("manage_lookup"), {"booking_number": ""})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.wsgi_request.session.get(""))

    def test_get_shows_blank_form(self):
        response = self.client.get(reverse("manage_lookup"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FIND MY APPOINTMENT")


class ManageBookingViewTests(TestCase):
    """Viewing an appointment's details via its booking number."""

    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.inspection_request = InspectionRequest.objects.first()
        self.target_date, self.target_time = next_available_slot()
        self.appointment = Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=self.target_date,
            time_label=self.target_time,
        )

    def test_shows_current_booking_details(self):
        response = self.client.get(
            reverse("manage_booking", args=[self.appointment.booking_number])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.appointment.booking_number)
        self.assertContains(
            response, escape(self.inspection_request.homeowner_names)
        )

    def test_unknown_booking_number_404s(self):
        response = self.client.get(
            reverse("manage_booking", args=["IRI-ZZZZ-ZZZZ"])
        )
        self.assertEqual(response.status_code, 404)

    def test_booking_number_lookup_in_url_is_case_insensitive(self):
        response = self.client.get(
            reverse("manage_booking", args=[self.appointment.booking_number.lower()])
        )
        self.assertEqual(response.status_code, 200)

    def test_cancelled_appointment_is_shown_read_only_not_404(self):
        self.appointment.status = Appointment.STATUS_CANCELLED
        self.appointment.save()

        response = self.client.get(
            reverse("manage_booking", args=[self.appointment.booking_number])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cancelled", msg_prefix="response should show cancelled state")
        # No reschedule form should be offered for a cancelled appointment.
        self.assertNotContains(response, 'id="schedule-form"')


class ManageRescheduleTests(TestCase):
    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.inspection_request = InspectionRequest.objects.first()
        self.original_date, self.original_time = next_available_slot()
        self.appointment = Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=self.original_date,
            time_label=self.original_time,
        )
        self.new_date, self.new_time = next_available_slot(start_offset=10)

    def _reschedule(self, date, time, confirmed=True):
        data = {"date": date.isoformat(), "time": time}
        if confirmed:
            data["confirmed"] = "on"
        return self.client.post(
            reverse("manage_reschedule", args=[self.appointment.booking_number]), data
        )

    def test_successful_reschedule_updates_the_appointment(self):
        response = self._reschedule(self.new_date, self.new_time)

        self.assertRedirects(
            response, reverse("manage_booking", args=[self.appointment.booking_number])
        )
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.date, self.new_date)
        self.assertEqual(self.appointment.time_label, self.new_time)

    def test_reschedule_frees_the_old_slot(self):
        self._reschedule(self.new_date, self.new_time)

        self.assertFalse(
            Appointment.objects.filter(
                date=self.original_date,
                time_label=self.original_time,
                status=Appointment.STATUS_SCHEDULED,
            ).exists()
        )

    def test_reschedule_resets_reminder_flags_for_the_new_time(self):
        now = timezone_now_for_tests()
        self.appointment.reminder_24h_sent_at = now
        self.appointment.reminder_1h_sent_at = now
        self.appointment.save()

        self._reschedule(self.new_date, self.new_time)

        self.appointment.refresh_from_db()
        self.assertIsNone(self.appointment.reminder_24h_sent_at)
        self.assertIsNone(self.appointment.reminder_1h_sent_at)

    def test_can_keep_the_same_slot_without_it_looking_taken_by_itself(self):
        response = self._reschedule(self.original_date, self.original_time)

        self.assertRedirects(
            response, reverse("manage_booking", args=[self.appointment.booking_number])
        )
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.date, self.original_date)
        self.assertEqual(self.appointment.time_label, self.original_time)

    def test_rescheduling_into_a_slot_someone_else_holds_is_rejected(self):
        other_request = InspectionRequest.objects.create(
            homeowner_names="Other Homeowner",
            phone_number="555-0100",
            email="other@example.com",
            property_address="456 Other St",
        )
        Appointment.objects.create(
            inspection_request=other_request,
            date=self.new_date,
            time_label=self.new_time,
        )

        response = self._reschedule(self.new_date, self.new_time)

        self.assertRedirects(
            response, reverse("manage_booking", args=[self.appointment.booking_number])
        )
        self.appointment.refresh_from_db()
        # Unchanged - still at the original slot.
        self.assertEqual(self.appointment.date, self.original_date)
        self.assertEqual(self.appointment.time_label, self.original_time)

    def test_missing_confirmation_checkbox_is_rejected(self):
        response = self._reschedule(self.new_date, self.new_time, confirmed=False)

        self.assertRedirects(
            response, reverse("manage_booking", args=[self.appointment.booking_number])
        )
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.date, self.original_date)

    def test_cannot_reschedule_a_cancelled_appointment(self):
        self.appointment.status = Appointment.STATUS_CANCELLED
        self.appointment.save()

        response = self._reschedule(self.new_date, self.new_time)

        self.assertRedirects(
            response, reverse("manage_booking", args=[self.appointment.booking_number])
        )
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Appointment.STATUS_CANCELLED)
        self.assertEqual(self.appointment.date, self.original_date)

    def test_reschedule_sends_notifications(self):
        mail.outbox.clear()
        self._reschedule(self.new_date, self.new_time)

        self.assertEqual(len(mail.outbox), 2)
        homeowner_email = next(
            m for m in mail.outbox if m.to == [self.inspection_request.email]
        )
        self.assertIn(self.appointment.booking_number, homeowner_email.body)
        self.assertIn(self.new_time, homeowner_email.body)

    def test_unknown_booking_number_404s(self):
        response = self.client.post(
            reverse("manage_reschedule", args=["IRI-ZZZZ-ZZZZ"]),
            {"date": self.new_date.isoformat(), "time": self.new_time, "confirmed": "on"},
        )
        self.assertEqual(response.status_code, 404)

    def test_get_is_not_allowed(self):
        response = self.client.get(
            reverse("manage_reschedule", args=[self.appointment.booking_number])
        )
        self.assertEqual(response.status_code, 405)


class ManageCancelTests(TestCase):
    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.inspection_request = InspectionRequest.objects.first()
        self.target_date, self.target_time = next_available_slot()
        self.appointment = Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=self.target_date,
            time_label=self.target_time,
        )

    def _cancel(self):
        return self.client.post(
            reverse("manage_cancel", args=[self.appointment.booking_number])
        )

    def test_cancel_marks_the_appointment_cancelled(self):
        response = self._cancel()

        self.assertRedirects(
            response, reverse("manage_booking", args=[self.appointment.booking_number])
        )
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Appointment.STATUS_CANCELLED)

    def test_cancel_frees_the_slot_for_someone_else(self):
        self._cancel()

        other_request = InspectionRequest.objects.create(
            homeowner_names="Other Homeowner",
            phone_number="555-0100",
            email="other@example.com",
            property_address="456 Other St",
        )
        rebooked = Appointment.objects.create(
            inspection_request=other_request,
            date=self.target_date,
            time_label=self.target_time,
        )
        self.assertEqual(rebooked.status, Appointment.STATUS_SCHEDULED)

    def test_cancelling_twice_shows_a_friendly_message_instead_of_erroring(self):
        self._cancel()
        response = self._cancel()

        self.assertRedirects(
            response,
            reverse("manage_booking", args=[self.appointment.booking_number]),
            fetch_redirect_response=False,
        )
        follow_up = self.client.get(
            reverse("manage_booking", args=[self.appointment.booking_number])
        )
        self.assertContains(follow_up, "already cancelled")

    def test_cancel_sends_notifications_to_homeowner_and_company(self):
        mail.outbox.clear()
        self._cancel()

        self.assertEqual(len(mail.outbox), 2)
        recipients = [m.to for m in mail.outbox]
        self.assertIn([self.inspection_request.email], recipients)
        self.assertIn([notifications.COMPANY_NOTIFICATION_EMAIL], recipients)

    def test_unknown_booking_number_404s(self):
        response = self.client.post(reverse("manage_cancel", args=["IRI-ZZZZ-ZZZZ"]))
        self.assertEqual(response.status_code, 404)

    def test_get_is_not_allowed(self):
        response = self.client.get(
            reverse("manage_cancel", args=[self.appointment.booking_number])
        )
        self.assertEqual(response.status_code, 405)


class ManageAvailabilityExclusionTests(TestCase):
    """The availability JSON endpoint's ?exclude= support for the reschedule calendar."""

    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.inspection_request = InspectionRequest.objects.first()
        self.target_date, self.target_time = next_available_slot()
        self.appointment = Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=self.target_date,
            time_label=self.target_time,
        )

    def test_own_slot_is_hidden_from_availability_by_default(self):
        response = self.client.get(
            reverse("schedule_availability"),
            {"month": self.target_date.strftime("%Y-%m")},
        )
        data = response.json()["available"]
        times_on_day = data.get(self.target_date.isoformat(), [])
        self.assertNotIn(self.target_time, times_on_day)

    def test_own_slot_reappears_when_excluded_by_booking_number(self):
        response = self.client.get(
            reverse("schedule_availability"),
            {
                "month": self.target_date.strftime("%Y-%m"),
                "exclude": self.appointment.booking_number,
            },
        )
        data = response.json()["available"]
        times_on_day = data.get(self.target_date.isoformat(), [])
        self.assertIn(self.target_time, times_on_day)

    def test_excluding_a_different_booking_number_has_no_effect(self):
        response = self.client.get(
            reverse("schedule_availability"),
            {
                "month": self.target_date.strftime("%Y-%m"),
                "exclude": "IRI-ZZZZ-ZZZZ",
            },
        )
        data = response.json()["available"]
        times_on_day = data.get(self.target_date.isoformat(), [])
        self.assertNotIn(self.target_time, times_on_day)


class ManageLinkNotificationTests(TestCase):
    """The "manage my appointment" link that rides along in every email."""

    def setUp(self):
        self.client.post(reverse("schedule_intake"), VALID_INTAKE_DATA)
        self.inspection_request = InspectionRequest.objects.first()
        self.target_date, self.target_time = next_available_slot()
        self.appointment = Appointment.objects.create(
            inspection_request=self.inspection_request,
            date=self.target_date,
            time_label=self.target_time,
        )

    def _manage_path(self):
        return reverse("manage_booking", args=[self.appointment.booking_number])

    def test_booking_confirmation_email_contains_manage_link(self):
        notifications.send_new_booking_notifications(self.appointment)

        homeowner_email = next(
            m for m in mail.outbox if m.to == [self.inspection_request.email]
        )
        self.assertIn(self._manage_path(), homeowner_email.body)

    def test_booking_confirmation_email_has_an_html_alternative_with_a_button(self):
        notifications.send_new_booking_notifications(self.appointment)

        homeowner_email = next(
            m for m in mail.outbox if m.to == [self.inspection_request.email]
        )
        self.assertEqual(len(homeowner_email.alternatives), 1)
        html_body, mimetype = homeowner_email.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn(self._manage_path(), html_body)
        self.assertIn("MANAGE MY APPOINTMENT", html_body)

    def test_company_email_also_contains_manage_link(self):
        notifications.send_new_booking_notifications(self.appointment)

        company_email = next(
            m for m in mail.outbox if m.to == [notifications.COMPANY_NOTIFICATION_EMAIL]
        )
        self.assertIn(self._manage_path(), company_email.body)

    def test_reminder_email_contains_manage_link(self):
        notifications.send_reminder(self.appointment, hours_before=24)

        homeowner_email = mail.outbox[0]
        self.assertIn(self._manage_path(), homeowner_email.body)
        self.assertEqual(len(homeowner_email.alternatives), 1)

    def test_cancellation_email_offers_a_link_to_schedule_a_new_inspection(self):
        notifications.send_cancellation_notifications(self.appointment)

        homeowner_email = next(
            m for m in mail.outbox if m.to == [self.inspection_request.email]
        )
        self.assertIn(reverse("schedule_intake"), homeowner_email.body)
        html_body, _ = homeowner_email.alternatives[0]
        self.assertIn("SCHEDULE A NEW INSPECTION", html_body)

    def test_text_only_preference_still_gets_the_manage_link_via_sms(self):
        text_request = InspectionRequest.objects.create(
            homeowner_names="Text Only Homeowner",
            phone_number="5555550199",
            email="textonly@example.com",
            property_address="789 Elm St",
            notification_preference=InspectionRequest.NOTIFY_TEXT,
        )
        other_date, other_time = next_available_slot(start_offset=15)
        text_appointment = Appointment.objects.create(
            inspection_request=text_request, date=other_date, time_label=other_time
        )

        with self.assertLogs("portfolioapp.notifications", level="WARNING"):
            notifications.send_new_booking_notifications(text_appointment)


def timezone_now_for_tests():
    from django.utils import timezone

    return timezone.now()