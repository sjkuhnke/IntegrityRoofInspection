from django.contrib import admin
from django.urls import path

from portfolioapp import views


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.home, name="home"),
    path("schedule/start/", views.schedule_intake, name="schedule_intake"),
    path("schedule/reset/", views.schedule_reset, name="schedule_reset"),
    path("schedule/", views.schedule, name="schedule"),
    path("schedule/availability/", views.schedule_availability, name="schedule_availability"),
    path("schedule/confirm/", views.schedule_confirmation, name="schedule_confirmation"),
    path("schedule/confirmed/<str:booking_number>/", views.schedule_confirmed, name="schedule_confirmed"),
    path("schedule/manage/", views.manage_lookup, name="manage_lookup"),
    path("schedule/manage/<str:booking_number>/", views.manage_booking, name="manage_booking"),
    path("schedule/manage/<str:booking_number>/reschedule/", views.manage_reschedule, name="manage_reschedule"),
    path("schedule/manage/<str:booking_number>/cancel/", views.manage_cancel, name="manage_cancel",),
]
