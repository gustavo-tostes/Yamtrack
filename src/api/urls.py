from django.urls import path

from api import mobile_lists, views

app_name = "api"

urlpatterns = [
    path("mobile/health/", views.mobile_health, name="mobile_health"),
    path("search/", views.mobile_search, name="mobile_search"),
    path("media/add/", views.mobile_media_add, name="mobile_media_add"),
    path("lists/", mobile_lists.mobile_lists, name="mobile_lists"),
    path("auth/login/", views.login, name="login"),
    path("auth/logout/", views.logout, name="logout"),
    path("me/", views.me, name="me"),
    path("home/next-up/", views.home_next_up, name="home_next_up"),
    path("media/<str:media_type>/<int:instance_id>/", views.media_detail, name="media_detail"),
    path("media/<str:media_type>/<int:instance_id>/progress/", views.media_progress, name="media_progress"),
]
