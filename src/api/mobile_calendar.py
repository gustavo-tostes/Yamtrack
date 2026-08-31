from datetime import date, timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from api.views import _get_authenticated_api_user
from events import tasks
from events.models import Event
from users.models import WeekStartDayChoices


def login_not_required(view_func):
    """Mark this API view as public for login-required middleware.

    Authentication is still handled manually through the Authorization header.
    """
    view_func.login_required = False
    return view_func


def _get_authenticated_user(request):
    auth_result = _get_authenticated_api_user(request)

    if isinstance(auth_result, JsonResponse):
        return None, auth_result

    if auth_result is None:
        return None, JsonResponse(
            {
                "detail": "Autenticação necessária.",
            },
            status=401,
        )

    return auth_result, None


def _parse_month_year(request):
    month = request.GET.get("month")
    year = request.GET.get("year")

    if month is None or year is None:
        today = timezone.localdate()
        return today.month, today.year

    try:
        month = int(month)
        year = int(year)

        if month < 1 or month > 12:
            raise ValueError

        if year < 1 or year > 9999:
            raise ValueError

        return month, year

    except (TypeError, ValueError):
        return None


def _get_month_range(month, year):
    first_day = date(year, month, 1)

    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)

    last_day = next_month - timedelta(days=1)

    return first_day, last_day


def _serialize_media_item(item):
    return {
        "id": item.id,
        "media_id": str(item.media_id),
        "source": item.source,
        "media_type": item.media_type,
        "title": item.title,
        "image": item.image,
        "season_number": getattr(item, "season_number", None),
    }


def _serialize_event(event):
    local_datetime = timezone.localtime(event.datetime)

    return {
        "id": event.id,
        "title": str(event),
        "date": local_datetime.date().isoformat(),
        "datetime": local_datetime.isoformat(),
        "time": (
            None
            if event.is_sentinel_time
            else local_datetime.strftime("%H:%M")
        ),
        "has_known_time": not event.is_sentinel_time,
        "content_number": event.content_number,
        "readable_content_number": event.readable_content_number,
        "media": _serialize_media_item(event.item),
    }


@login_not_required
@require_GET
def mobile_calendar(request):
    user, error_response = _get_authenticated_user(request)

    if error_response:
        return error_response

    parsed_date = _parse_month_year(request)

    if parsed_date is None:
        return JsonResponse(
            {
                "success": False,
                "error": "Mês ou ano inválido.",
            },
            status=400,
        )

    month, year = parsed_date

    try:
        first_day, last_day = _get_month_range(
            month,
            year,
        )
    except ValueError:
        return JsonResponse(
            {
                "success": False,
                "error": "Não foi possível consultar esse período.",
            },
            status=400,
        )

    releases = Event.objects.get_user_events(
        user,
        first_day,
        last_day,
    )

    serialized_events = [
        _serialize_event(release)
        for release in releases
    ]

    week_starts_on = (
        "sunday"
        if user.week_start_day == WeekStartDayChoices.SUNDAY
        else "monday"
    )

    return JsonResponse(
        {
            "month": month,
            "year": year,
            "today": timezone.localdate().isoformat(),
            "week_starts_on": week_starts_on,
            "count": len(serialized_events),
            "events": serialized_events,
        },
        status=200,
    )


@login_not_required
@csrf_exempt
@require_POST
def mobile_calendar_reload(request):
    user, error_response = _get_authenticated_user(request)

    if error_response:
        return error_response

    task = tasks.reload_calendar.delay(user)

    return JsonResponse(
        {
            "success": True,
            "message": "A atualização do calendário foi iniciada.",
            "task_id": str(task.id),
        },
        status=202,
    )