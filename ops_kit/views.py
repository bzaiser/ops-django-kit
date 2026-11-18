import shutil

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render

from .models import AuditLog


def healthz_view(request):
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        db_ok = False

    base_dir = str(getattr(settings, "BASE_DIR", "."))
    disk_free_mb = round(shutil.disk_usage(base_dir).free / (1024 * 1024), 1)
    status_code = 200 if db_ok else 503
    return JsonResponse(
        {
            "status": "ok" if db_ok else "error",
            "db": db_ok,
            "disk_free_mb": disk_free_mb,
            "app": getattr(settings, "DESIGN_STUDIO_APP_NAME", "App"),
        },
        status=status_code,
    )


@staff_member_required
def audit_log_view(request):
    logs = AuditLog.objects.select_related("user")[:200]
    return render(request, "ops_kit/audit_log.html", {"logs": logs})
