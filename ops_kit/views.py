import shutil

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from .models import AuditLog, UpdateSettings
from .updater import apply_nas_updates, check_github_updates, is_desktop_frozen


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


@staff_member_required
def updates_view(request):
    cfg = UpdateSettings.get_solo()
    if request.method == "POST":
        action = request.POST.get("action", "save_config")
        if action == "save_config":
            cfg.enabled = bool(request.POST.get("enabled"))
            cfg.check_on_startup = bool(request.POST.get("check_on_startup"))
            cfg.auto_apply_kits = bool(request.POST.get("auto_apply_kits"))
            try:
                cfg.check_interval_hours = max(1, min(168, int(request.POST.get("check_interval_hours") or 24)))
            except (TypeError, ValueError):
                cfg.check_interval_hours = 24
            cfg.github_repo = (request.POST.get("github_repo") or "").strip()[:160]
            cfg.save()
            messages.success(request, "Update-Einstellungen gespeichert.")
        elif action == "check_now":
            res = check_github_updates(cfg, force=True)
            if res.get("has_updates"):
                messages.warning(request, "Neue Updates auf GitHub verfügbar.")
            else:
                messages.success(request, "Alles aktuell.")
        elif action == "apply_now":
            if is_desktop_frozen():
                messages.info(request, "Im Desktop-Modus (.exe) bitte das Release über den Download-Link installieren.")
            else:
                try:
                    apply_nas_updates(cfg, reload_gunicorn=True, user=request.user)
                    messages.success(request, "Updates erfolgreich eingespielt und Server neu geladen.")
                except Exception as exc:
                    messages.error(request, f"Update fehlgeschlagen: {exc}")
        next_url = (request.POST.get("next") or "").strip()
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        return redirect("ops_kit:updates")

    return render(request, "ops_kit/updates.html", get_updates_context())


def get_updates_context():
    cfg = UpdateSettings.get_solo()
    result = cfg.last_result_json or {}
    return {
        "cfg": cfg,
        "items": result.get("items") or [],
        "has_updates": bool(result.get("has_updates")),
        "is_desktop": is_desktop_frozen(),
    }
