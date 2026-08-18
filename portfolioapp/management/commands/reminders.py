"""
Send 24-hour and 1-hour reminders for upcoming appointments.

This is NOT a long-running process - it checks once and exits. Run it
periodically (every 5-15 minutes is plenty) via:

  * Windows: Task Scheduler, "Start a program":
      Program: C:\\path\\to\\venv\\Scripts\\python.exe
      Arguments: manage.py reminders
      Start in: C:\\path\\to\\project

Idempotent by design: each Appointment tracks reminder_24h_sent_at /
reminder_1h_sent_at, so running this every 5 minutes (or catching up
after being down for a while) never double-sends. If it hasn't run in
a while and an appointment is now less than 24 hours out, it just sends
the 24-hour reminder a bit "late" rather than skipping it.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from portfolioapp import notifications
from portfolioapp.availability import BUSINESS_TIMEZONE
from portfolioapp.models import Appointment


class Command(BaseCommand):
    help = "Send 24-hour and 1-hour reminder notifications for upcoming appointments."

    def handle(self, *args, **options):
        now = timezone.now().astimezone(BUSINESS_TIMEZONE)

        appointments = Appointment.objects.filter(
            status=Appointment.STATUS_SCHEDULED
        ).select_related("inspection_request")

        sent_24h = 0
        sent_1h = 0

        for appointment in appointments:
            remaining = appointment.start_datetime - now

            if remaining <= timedelta(0):
                continue  # already started or passed - nothing to remind about

            if (
                appointment.reminder_24h_sent_at is None
                and remaining <= timedelta(hours=24)
            ):
                notifications.send_reminder(appointment, hours_before=24)
                appointment.reminder_24h_sent_at = timezone.now()
                appointment.save(update_fields=["reminder_24h_sent_at"])
                sent_24h += 1

            if (
                appointment.reminder_1h_sent_at is None
                and remaining <= timedelta(hours=1)
            ):
                notifications.send_reminder(appointment, hours_before=1)
                appointment.reminder_1h_sent_at = timezone.now()
                appointment.save(update_fields=["reminder_1h_sent_at"])
                sent_1h += 1

        self.stdout.write(
            f"Sent {sent_24h} 24-hour reminder(s) and {sent_1h} 1-hour reminder(s)."
        )
