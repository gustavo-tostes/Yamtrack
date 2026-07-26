from django.urls import path

from api import views

app_name = "api"

urlpatterns = [
    path("auth/login/", views.login, name="login"),
    path("auth/logout/", views.logout, name="logout"),
    path("me/", views.me, name="me"),
    path("home/next-up/", views.home_next_up, name="home_next_up"),
    path("media/<str:media_type>/<int:instance_id>/progress/", views.media_progress, name="media_progress"),
]
