from django.urls import path

from . import views

app_name = "ops_kit"

urlpatterns = [
    path("healthz/", views.healthz_view, name="healthz"),
    path("audit/", views.audit_log_view, name="audit_log"),
]
