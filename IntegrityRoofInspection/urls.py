from django.contrib import admin
from django.urls import path

from portfolioapp import views


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.home, name="home"),
    path("schedule/", views.schedule, name="schedule"),
    path("schedule/confirm/", views.schedule_confirmation, name="schedule_confirmation")
]
