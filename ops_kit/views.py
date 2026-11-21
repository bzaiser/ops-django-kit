import shutil
import threading

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection, connections
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from .models import AuditLog, UpdateSettings
from .updater import (
    apply_nas_updates,
    check_github_updates,
    get_optional_kits_catalog,
    install_kit_package,
    is_desktop_frozen,
)


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


_UPDATE_THREAD = None


def _is_update_thread_alive() -> bool:
    return bool(_UPDATE_THREAD is not None and _UPDATE_THREAD.is_alive())


@staff_member_required
def updates_view(request):
    global _UPDATE_THREAD
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
            elif cfg.last_status == "running" and _is_update_thread_alive():
                messages.info(request, "Update läuft bereits im Hintergrund.")
            else:
                cfg.last_status = "running"
                items = (cfg.last_result_json or {}).get("items") or []
                upd_targets = [i.get("repo", i.get("key")) for i in items if i.get("update_available")]
                target_str = ", ".join(upd_targets) if upd_targets else "App & Pakete"
                cfg.last_log = f"Update gestartet ({target_str}) — bitte warten..."
                cfg.save(update_fields=["last_status", "last_log"])
                user_id = getattr(request.user, "pk", None)

                def _bg_apply():
                    try:
                        from django.contrib.auth import get_user_model
                        u = get_user_model().objects.filter(pk=user_id).first() if user_id else None
                        apply_nas_updates(UpdateSettings.get_solo(), reload_gunicorn=True, user=u)
                    except Exception as exc:
                        try:
                            c = UpdateSettings.get_solo()
                            c.last_status = "error"
                            c.last_log = f"Update fehlgeschlagen: {exc}"
                            c.save(update_fields=["last_status", "last_log"])
                        except Exception:
                            pass
                    finally:
                        connections.close_all()

                _UPDATE_THREAD = threading.Thread(target=_bg_apply, daemon=True)
                _UPDATE_THREAD.start()
                messages.info(request, "Update läuft im Hintergrund — Seite aktualisiert sich automatisch.")
        elif action == "install_kit":
            kit_key = (request.POST.get("kit_key") or "").strip()
            mods = request.POST.getlist("domain_modules")
            if kit_key == "domain_kit" and mods:
                profile = ",".join(mods)
            else:
                profile = (request.POST.get("profile") or "all").strip()
            if is_desktop_frozen():
                messages.info(request, "Nachinstallation im Desktop-Modus (.exe) nicht verfügbar.")
            elif cfg.last_status == "running" and _is_update_thread_alive():
                messages.info(request, "Ein Installationsvorgang läuft bereits im Hintergrund.")
            else:
                cfg.last_status = "running"
                cfg.last_log = f"Installiere {kit_key} (Profil: {profile}) von GitHub..."
                cfg.save(update_fields=["last_status", "last_log"])
                user_id = getattr(request.user, "pk", None)

                def _bg_install():
                    try:
                        from django.contrib.auth import get_user_model
                        u = get_user_model().objects.filter(pk=user_id).first() if user_id else None
                        install_kit_package(kit_key, user=u, profile=profile)
                    except Exception as exc:
                        try:
                            c = UpdateSettings.get_solo()
                            c.last_status = "error"
                            c.last_log = f"Installation von {kit_key} fehlgeschlagen: {exc}"
                            c.save(update_fields=["last_status", "last_log"])
                        except Exception:
                            pass
                    finally:
                        connections.close_all()

                _UPDATE_THREAD = threading.Thread(target=_bg_install, daemon=True)
                _UPDATE_THREAD.start()
                messages.info(request, f"Installiere {kit_key} im Hintergrund — Seite aktualisiert sich automatisch.")
        elif action == "save_domain_modules":
            try:
                from django.apps import apps
                if apps.is_installed("domain_kit"):
                    DMS = apps.get_model("domain_kit", "DomainModuleSettings")
                    solo = DMS.get_solo()
                    mods = request.POST.getlist("domain_modules")
                    solo.enable_master_data = "master_data" in mods
                    solo.enable_persons = "persons" in mods
                    solo.enable_addresses = "persons" in mods
                    solo.enable_documents = "documents" in mods
                    solo.enable_assets = "assets" in mods
                    solo.enable_inventory = "assets" in mods
                    solo.enable_contracts = "assets" in mods
                    solo.enable_accounts = "assets" in mods
                    solo.enable_global_search = "global_search" in mods
                    solo.save()
                    messages.success(request, "Stammdaten-Teilbereiche erfolgreich gespeichert.")
                else:
                    messages.warning(request, "Domain-Kit ist nicht installiert.")
            except Exception as exc:
                messages.error(request, f"Fehler beim Speichern der Teilbereiche: {exc}")
        next_url = (request.POST.get("next") or "").strip()
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        return redirect("ops_kit:updates")

    return render(request, "ops_kit/updates.html", get_updates_context())


def get_updates_context():
    cfg = UpdateSettings.get_solo()
    result = cfg.last_result_json or {}
    domain_installed = "domain_kit" in set(getattr(settings, "INSTALLED_APPS", []))
    domain_modules = None
    if domain_installed:
        try:
            from django.apps import apps
            if apps.is_installed("domain_kit"):
                DMS = apps.get_model("domain_kit", "DomainModuleSettings")
                if DMS and hasattr(DMS, "get_solo"):
                    solo = DMS.get_solo()
                    domain_modules = {
                        "master_data": bool(solo.enable_master_data),
                        "persons": bool(solo.enable_persons),
                        "documents": bool(solo.enable_documents),
                        "assets": bool(solo.enable_assets),
                        "global_search": bool(solo.enable_global_search),
                    }
        except Exception:
            domain_modules = None

    return {
        "cfg": cfg,
        "items": result.get("items") or [],
        "has_updates": bool(result.get("has_updates")),
        "is_desktop": is_desktop_frozen(),
        "optional_kits": get_optional_kits_catalog(),
        "domain_installed": domain_installed,
        "domain_modules": domain_modules,
    }


@staff_member_required
def status_json_view(request):
    cfg = UpdateSettings.get_solo()
    return JsonResponse({
        "status": cfg.last_status,
        "log": cfg.last_log or "",
        "last_checked_at": cfg.last_checked_at.isoformat() if cfg.last_checked_at else None,
    })
