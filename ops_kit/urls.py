from django.urls import path

from . import views

app_name = "ops_kit"

urlpatterns = [
    path("healthz/", views.healthz_view, name="healthz"),
    path("audit/", views.audit_log_view, name="audit_log"),
    path("updates/", views.updates_view, name="updates"),
    path("backups/", views.backups_view, name="backups"),
    path("backups/start/", views.backup_start_view, name="backup_start"),
    path("backups/status/<str:job_id>/", views.backup_status_view, name="backup_status"),
    path("backups/download/<str:filename>/", views.backup_download_view, name="backup_download"),
    path("status.json", views.status_json_view, name="status_json"),
]
