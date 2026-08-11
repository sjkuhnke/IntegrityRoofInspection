import secrets

from django.shortcuts import render


def home(request):
    return render(request, "home.html")


def schedule(request):
    return render(request, "schedule.html")


def schedule_confirmation(request):
    if request.method != "POST":
        return render(request, "schedule.html")

    inspection_date = request.POST.get("date")
    inspection_time = request.POST.get("time")

    booking_number = generate_booking_number()

    context = {
        "inspection_date": inspection_date,
        "inspection_time": inspection_time,
        "booking_number": booking_number,
    }

    return render(
        request,
        "schedule_confirm.html",
        context,
    )


def generate_booking_number():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    parts = [
        "".join(secrets.choice(alphabet) for _ in range(4)),
        "".join(secrets.choice(alphabet) for _ in range(4)),
    ]

    return "IRI-" + "-".join(parts)
