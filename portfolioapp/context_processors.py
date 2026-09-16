from django.urls import reverse

SCHEDULE_FLOW_URL_NAMES = {
    'schedule_home', 'schedule_intake', 'schedule',
    'schedule_confirmed', 'manage_lookup', 'manage_booking',
}


def schedule_flow(request):
    url_name = getattr(request.resolver_match, 'url_name', None)
    return {'in_schedule_flow': url_name in SCHEDULE_FLOW_URL_NAMES}


def marketing_ctas(request):
    return {
        'schedule_marketing_url': f"{reverse('schedule')}?src=marketing",
    }
