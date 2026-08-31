from django.urls import path

from api import mobile_calendar, mobile_lists, views

app_name = "api"

urlpatterns = [
    path(
        "mobile/health/",
        views.mobile_health,
        name="mobile_health",
    ),
    path(
        "search/",
        views.mobile_search,
        name="mobile_search",
    ),
    path(
        "media/add/",
        views.mobile_media_add,
        name="mobile_media_add",
    ),
    path(
        "lists/",
        mobile_lists.mobile_lists,
        name="mobile_lists",
    ),
    path(
        "lists/<int:list_id>/",
        mobile_lists.mobile_list_detail,
        name="mobile_list_detail",
    ),
    path(
        "calendar/",
        mobile_calendar.mobile_calendar,
        name="mobile_calendar",
    ),
    path(
        "calendar/reload/",
        mobile_calendar.mobile_calendar_reload,
        name="mobile_calendar_reload",
    ),
    path(
        "auth/login/",
        views.login,
        name="login",
    ),
    path(
        "auth/logout/",
        views.logout,
        name="logout",
    ),
    path(
        "me/",
        views.me,
        name="me",
    ),
    path(
        "home/next-up/",
        views.home_next_up,
        name="home_next_up",
    ),
    path(
        "media/<str:media_type>/<int:instance_id>/",
        views.media_detail,
        name="media_detail",
    ),
    path(
        "media/<str:media_type>/<int:instance_id>/progress/",
        views.media_progress,
        name="media_progress",
    ),
]