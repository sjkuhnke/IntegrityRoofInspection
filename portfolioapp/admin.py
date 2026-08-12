from django.contrib import admin

from .models import Appointment, InspectionRequest


@admin.register(InspectionRequest)
class InspectionRequestAdmin(admin.ModelAdmin):
    list_display = ("homeowner_names", "phone_number", "email", "property_address", "created_at")
    search_fields = ("homeowner_names", "phone_number", "email", "property_address")


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("booking_number", "date", "time_label", "status", "inspection_request", "created_at")
    list_filter = ("status", "date")
    search_fields = ("booking_number", "inspection_request__homeowner_names", "inspection_request__email")