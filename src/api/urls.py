from django.urls import path

from api import views

app_name = "api"

urlpatterns = [
    path("auth/login/", views.login, name="login"),
    path("auth/logout/", views.logout, name="logout"),
    path("me/", views.me, name="me"),
]
