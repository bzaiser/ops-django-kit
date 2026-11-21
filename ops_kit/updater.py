from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from .models import UpdateSettings, log_audit

KIT_GITHUB_REPOS = {
    "design_studio": "bzaiser/django-design-studio",
    "domain_kit": "bzaiser/domain-django-kit",
    "auth_kit": "bzaiser/auth-django-kit",
    "notify_kit": "bzaiser/notify-django-kit",
    "cal_kit": "bzaiser/cal-django-kit",
    "grid_kit": "bzaiser/grid-django-kit",
    "seed_kit": "bzaiser/seed-django-kit",
    "ops_kit": "bzaiser/ops-django-kit",
}


def is_desktop_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_local_app_version() -> str:
    base_dir = Path(getattr(settings, "BASE_DIR", "."))
    vfile = base_dir / "VERSION"
    if vfile.exists():
        try:
            return vfile.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


def get_local_app_commit() -> str:
    base_dir = Path(getattr(settings, "BASE_DIR", "."))
    if (base_dir / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(base_dir),
                capture_output=True,
                text=True,
                timeout=3,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()[:12]
        except Exception:
            pass
    return ""


def _update_progress(cfg: UpdateSettings, message: str, status: str = "running"):
    try:
        cfg.last_status = status
        lines = (cfg.last_log or "").splitlines()
        if not any(message == l for l in lines):
            cfg.last_log = ((cfg.last_log + "\n" if cfg.last_log else "") + message)[-4000:]
        cfg.save(update_fields=["last_status", "last_log"])
    except Exception:
        pass


def _default_fetch_json(url: str, timeout: int = 4) -> dict:
    import os
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ops-django-kit-updater/1.0",
    }
    token = (os.environ.get("OPS_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_remote_head_sha(repo: str, timeout: int = 5) -> str:
    """Use `git ls-remote` to read main HEAD SHA without GitHub REST API rate limits."""
    try:
        proc = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}.git", "refs/heads/main"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        line = (proc.stdout or "").strip().splitlines()
        if line:
            return line[0].split()[0][:12]
    except Exception:
        pass
    return ""


def should_run_startup_check(cfg: UpdateSettings) -> bool:
    if not cfg.enabled or not cfg.check_on_startup:
        return False
    if not cfg.last_checked_at:
        return True
    interval = max(1, int(cfg.check_interval_hours or 24))
    return timezone.now() >= cfg.last_checked_at + timedelta(hours=interval)


def active_kit_repos() -> list[tuple[str, str]]:
    installed = set(getattr(settings, "INSTALLED_APPS", []))
    return [(k, repo) for k, repo in KIT_GITHUB_REPOS.items() if k in installed]


KIT_METADATA = {
    "design_studio": ("Design Studio", ""),
    "domain_kit": ("Stammdaten / Domain Kit", 'path("domain/", include("domain_kit.urls")),'),
    "auth_kit": ("Auth / Profil Kit", 'path("accounts/", include("auth_kit.urls")),'),
    "notify_kit": ("Benachrichtigungen", 'path("notifications/", include("notify_kit.urls")),'),
    "cal_kit": ("Kalender", 'path("calendar/", include("cal_kit.urls")),'),
    "grid_kit": ("DataGrid-Tabellen", ""),
    "seed_kit": ("Demo-Daten Seeder", ""),
    "ops_kit": ("Ops & Auto-Updates", 'path("ops/", include("ops_kit.urls")),'),
}


def _installed_apps_from_disk() -> set[str]:
    """Read INSTALLED_APPS from the project's settings.py on disk (not in-memory Django settings)."""
    import re
    base_dir = Path(getattr(settings, "BASE_DIR", "."))
    settings_mod = getattr(settings, "SETTINGS_MODULE", None) or ""
    if settings_mod:
        settings_path = base_dir.joinpath(*settings_mod.split(".")).with_suffix(".py")
    else:
        settings_path = base_dir / "config" / "settings.py"
    if not settings_path.exists():
        # fallback to glob
        candidates = list(base_dir.rglob("settings.py"))
        settings_path = candidates[0] if candidates else None
    if not settings_path or not settings_path.exists():
        return set()
    txt = settings_path.read_text(encoding="utf-8")
    # Extract all quoted strings inside INSTALLED_APPS = [ ... ]
    m = re.search(r"INSTALLED_APPS\s*=\s*\[([\s\S]*?)\]", txt)
    if not m:
        return set()
    block = m.group(1)
    return set(re.findall(r"""['"]([^'"]+)['"]""", block))


def get_optional_kits_catalog() -> list[dict]:
    installed_apps = _installed_apps_from_disk()
    # Also merge in-memory (already-loaded apps) to be safe
    installed_apps |= set(getattr(settings, "INSTALLED_APPS", []))
    catalog = []
    for key, repo in KIT_GITHUB_REPOS.items():
        if key in installed_apps:
            continue
        label, url_snippet = KIT_METADATA.get(key, (key, ""))
        pip_installed = importlib.util.find_spec(key) is not None
        catalog.append({
            "key": key,
            "label": label,
            "repo": repo,
            "pip_installed": pip_installed,
            "url_snippet": url_snippet,
        })
    return catalog


def _auto_wire_kit_in_project(kit_key: str, url_snippet: str) -> list[str]:
    """Safely add kit_key to INSTALLED_APPS and url_snippet to urls.py if not already present."""
    import os
    import re
    notes = []
    base_dir = Path(getattr(settings, "BASE_DIR", "."))
    settings_mod = getattr(settings, "SETTINGS_MODULE", None) or os.environ.get("DJANGO_SETTINGS_MODULE", "") or ""
    if settings_mod:
        settings_path = base_dir.joinpath(*settings_mod.split(".")).with_suffix(".py")
    else:
        settings_path = base_dir / "config" / "settings.py"
    if not settings_path.exists():
        candidates = list(base_dir.rglob("settings.py"))
        if candidates:
            settings_path = candidates[0]

    root_urlconf = str(getattr(settings, "ROOT_URLCONF", "") or "")
    if root_urlconf:
        urls_path = base_dir.joinpath(*root_urlconf.split(".")).with_suffix(".py")
    else:
        urls_path = base_dir / "config" / "urls.py"
    if not urls_path.exists():
        candidates = list(base_dir.rglob("urls.py"))
        if candidates:
            urls_path = candidates[0]

    if settings_path.exists():
        txt = settings_path.read_text(encoding="utf-8")
        if f'"{kit_key}"' not in txt and f"'{kit_key}'" not in txt:
            m = re.search(r"(INSTALLED_APPS\s*=\s*\[[\s\S]*?)(\n\])", txt)
            if m:
                new_txt = txt[:m.end(1)] + f'\n    "{kit_key}",' + txt[m.start(2):]
                settings_path.write_text(new_txt, encoding="utf-8")
                notes.append(f"settings.py: '{kit_key}' zu INSTALLED_APPS hinzugefügt.")

    if url_snippet and urls_path.exists():
        utxt = urls_path.read_text(encoding="utf-8")
        if f"{kit_key}.urls" not in utxt:
            if "include" not in utxt and "from django.urls import" in utxt:
                utxt = re.sub(r"from django\.urls import ([^\n]+)", lambda r: f"from django.urls import include, {r.group(1)}", utxt, count=1)
            # Insert before root catchall (e.g. path('', ...)) if present so kit prefix routes take precedence
            m_catchall = re.search(r"(\n\s*(?:re_)?path\(\s*['\"]['\"],)", utxt)
            if m_catchall:
                insert_pos = m_catchall.start(1)
                new_utxt = utxt[:insert_pos] + f"\n    {url_snippet}" + utxt[insert_pos:]
                urls_path.write_text(new_utxt, encoding="utf-8")
                notes.append(f"urls.py: {url_snippet} hinzugefügt.")
            else:
                m = re.search(r"(urlpatterns\s*=\s*\[[\s\S]*?)(\n\])", utxt)
                if m:
                    new_utxt = utxt[:m.end(1)] + f"\n    {url_snippet}" + utxt[m.start(2):]
                    urls_path.write_text(new_utxt, encoding="utf-8")
                    notes.append(f"urls.py: {url_snippet} hinzugefügt.")

    if kit_key == "domain_kit" and settings_path.exists():
        stxt = settings_path.read_text(encoding="utf-8")
        if "DESIGN_STUDIO_MENU_ITEMS" in stxt and "domain_kit:" not in stxt:
            domain_menu = """    {"id": "master_data", "url_name": "domain_kit:settings", "match_urls": ["settings"], "default_label": "Stammdaten", "icon": "bi-tags", "zone": "main", "display": "icon_text", "visible": True, "order": 10},
    {"id": "persons", "url_name": "domain_kit:persons", "match_urls": ["persons"], "default_label": "Personen", "icon": "bi-people", "zone": "main", "display": "icon_text", "visible": True, "order": 11},
    {"id": "assets", "url_name": "domain_kit:assets", "match_urls": ["assets"], "default_label": "Assets", "icon": "bi-boxes", "zone": "main", "display": "icon_text", "visible": True, "order": 12},
    {"id": "documents", "url_name": "domain_kit:documents", "match_urls": ["documents"], "default_label": "Dokumente", "icon": "bi-folder2-open", "zone": "main", "display": "icon_text", "visible": True, "order": 13},
    {"id": "search", "url_name": "domain_kit:search", "match_urls": ["search"], "default_label": "Suche", "icon": "bi-search", "zone": "main", "display": "icon_text", "visible": True, "order": 14},"""
            m_menu = re.search(r"(DESIGN_STUDIO_MENU_ITEMS\s*=\s*\[[\s\S]*?)(\n\])", stxt)
            if m_menu:
                new_stxt = stxt[:m_menu.end(1)] + "\n" + domain_menu + stxt[m_menu.start(2):]
                settings_path.write_text(new_stxt, encoding="utf-8")
                notes.append("settings.py: Domain-Kit Menüeinträge zu DESIGN_STUDIO_MENU_ITEMS hinzugefügt.")
    return notes


def restart_server_or_container(delay_seconds: float = 1.0):
    """Gracefully restart web container or Gunicorn master so newly installed
    kits, settings, and code changes take full effect."""
    import signal
    import threading
    import time

    def _do_restart():
        time.sleep(delay_seconds)
        # 1. Gunicorn worker: signal master process if present (master is PID 1 in container with exec)
        ppid = os.getppid()
        if ppid >= 1 and hasattr(signal, "SIGHUP"):
            try:
                os.kill(ppid, signal.SIGHUP)
            except Exception:
                pass
        # 2. Docker: kill PID 1 so Docker (restart: unless-stopped) does a fresh container reload
        is_docker = (
            Path("/.dockerenv").exists()
            or Path("/opt/ds-pkg").exists()
            or bool(os.environ.get("RUNNING_IN_DOCKER"))
        )
        if is_docker:
            try:
                if hasattr(signal, "SIGTERM"):
                    os.kill(1, signal.SIGTERM)
                time.sleep(1.0)
                if hasattr(signal, "SIGKILL"):
                    os.kill(1, signal.SIGKILL)
            except Exception:
                pass

    t = threading.Thread(target=_do_restart, daemon=True)
    t.start()


def install_kit_package(kit_key: str, user=None, reload_gunicorn: bool = True, profile: str = "all") -> str:
    if kit_key not in KIT_GITHUB_REPOS:
        raise ValueError(f"Unbekanntes Kit: {kit_key}")
    if is_desktop_frozen():
        return "Desktop-Modus (.exe): Nachinstallation im Bundle nicht unterstützt."
    import os
    cfg = UpdateSettings.get_solo()
    _update_progress(cfg, f"▶ [1/3] Lade Paket '{kit_key}' von GitHub herunter...")

    repo = KIT_GITHUB_REPOS[kit_key]
    zip_url = f"https://github.com/{repo}/archive/refs/heads/main.zip"
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-deps", "--no-cache-dir"]
    target_dir = Path("/opt/ds-pkg")
    if target_dir.exists() and os.access(target_dir, os.W_OK):
        cmd.extend(["--target", str(target_dir)])
    cmd.append(zip_url)
    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180, check=True)
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")

    _update_progress(cfg, f"▶ [2/3] Konfiguriere '{kit_key}' in settings.py und führe Migrationen aus...")
    _, url_snippet = KIT_METADATA.get(kit_key, (kit_key, ""))
    try:
        wire_notes = _auto_wire_kit_in_project(kit_key, url_snippet)
        if wire_notes:
            out += "\n" + "\n".join(wire_notes)
    except Exception as exc:
        out += f"\nAuto-Aktivierung Warnung: {exc}"

    manage_py = Path(getattr(settings, "BASE_DIR", ".")) / "manage.py"
    if manage_py.exists():
        has_migrate_all = (Path(getattr(settings, "BASE_DIR", ".")) / "core" / "management" / "commands" / "migrate_all.py").exists()
        mig_subcommand = "migrate_all" if has_migrate_all else "migrate"
        for mcmd in (
            [sys.executable, str(manage_py), mig_subcommand] if mig_subcommand == "migrate_all" else [sys.executable, str(manage_py), mig_subcommand, "--noinput"],
            [sys.executable, str(manage_py), "collectstatic", "--clear", "--noinput"],
        ):
            try:
                mp = subprocess.run(mcmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
                if mp.stdout:
                    out += "\n" + mp.stdout
                if mp.stderr:
                    out += "\n" + mp.stderr
            except Exception as exc:
                out += f"\n{mcmd[2]} Hinweis: {exc}"

        if kit_key == "domain_kit" and profile:
            profile_code = f"from domain_kit.models import DomainModuleSettings; DomainModuleSettings.get_solo().apply_profile({repr(profile)})"
            try:
                subprocess.run([sys.executable, str(manage_py), "shell", "-c", profile_code], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
                out += f"\nDomain-Kit Profil '{profile}' angewendet."
            except Exception as exc:
                out += f"\nDomain-Kit Profil Hinweis: {exc}"

    _update_progress(cfg, f"▶ [3/3] Installation von '{kit_key}' abgeschlossen! Starte Server neu...")
    cfg = UpdateSettings.get_solo()
    cfg.last_status = "updated"
    cfg.last_log = f"{cfg.last_log}\n\nDetail-Protokoll:\n{out}"[-4000:]
    cfg.save(update_fields=["last_status", "last_log"])
    log_audit(user, "KIT_INSTALLED", target=kit_key, details=out[:500])

    if reload_gunicorn:
        restart_server_or_container()
    return out


def check_github_updates(cfg: UpdateSettings | None = None, force: bool = False, fetch_json=None) -> dict:
    cfg = cfg or UpdateSettings.get_solo()
    if not force and not cfg.enabled:
        return cfg.last_result_json or {"status": "disabled", "items": [], "has_updates": False}

    fetcher = fetch_json or _default_fetch_json
    known = dict(cfg.installed_commits_json or {})
    items = []
    has_updates = False
    errors = []

    for key, repo in active_kit_repos():
        url = f"https://api.github.com/repos/{repo}/commits/main"
        try:
            remote_sha = _fetch_remote_head_sha(repo) if fetch_json is None else ""
            commit_msg = ""
            if not remote_sha:
                data = fetcher(url)
                remote_sha = str(data.get("sha") or "")[:12]
                commit_msg = str(((data.get("commit") or {}).get("message") or "")).splitlines()[0][:90]
            local_sha = str(known.get(key) or "")[:12]
            if not local_sha and remote_sha:
                # Erster Baseline-Scan: aktuellen Stand merken, wenn noch nichts hinterlegt ist
                known[key] = remote_sha
                local_sha = remote_sha
            update_avail = bool(remote_sha and local_sha and remote_sha != local_sha)
            if update_avail:
                has_updates = True
            items.append({
                "key": key,
                "repo": repo,
                "kind": "kit",
                "local": local_sha,
                "remote": remote_sha,
                "message": commit_msg,
                "update_available": update_avail,
                "url": f"https://github.com/{repo}",
            })
        except Exception as exc:
            errors.append(f"{key}: {exc}")
            items.append({
                "key": key,
                "repo": repo,
                "kind": "kit",
                "local": str(known.get(key) or "")[:12],
                "remote": "",
                "message": f"Prüfung fehlgeschlagen: {exc}",
                "update_available": False,
                "url": f"https://github.com/{repo}",
            })

    app_repo = (cfg.github_repo or "").strip().strip("/")
    if app_repo and "/" in app_repo:
        local_ver = get_local_app_version()
        local_git_head = get_local_app_commit()
        rel_info = {
            "key": "app",
            "repo": app_repo,
            "kind": "app",
            "local": local_git_head or local_ver or str(known.get("app") or "")[:12],
            "remote": "",
            "message": "",
            "update_available": False,
            "url": f"https://github.com/{app_repo}/releases",
            "download_url": "",
        }
        if is_desktop_frozen():
            try:
                rel = fetcher(f"https://api.github.com/repos/{app_repo}/releases/latest")
                tag = str(rel.get("tag_name") or "").strip()
                assets = rel.get("assets") or []
                dl_url = str(assets[0].get("browser_download_url") or "") if assets else str(rel.get("html_url") or "")
                rel_info["remote"] = tag
                rel_info["url"] = str(rel.get("html_url") or rel_info["url"])
                rel_info["download_url"] = dl_url
                rel_info["message"] = str(rel.get("name") or tag)[:90]
                if tag and local_ver and tag.lstrip("v") != local_ver.lstrip("v"):
                    rel_info["update_available"] = True
                    has_updates = True
            except Exception as exc:
                errors.append(f"app: {exc}")
        else:
            try:
                rsha = _fetch_remote_head_sha(app_repo) if fetch_json is None else ""
                cmsg = ""
                if not rsha:
                    cdata = fetcher(f"https://api.github.com/repos/{app_repo}/commits/main")
                    rsha = str(cdata.get("sha") or "")[:12]
                    cmsg = str(((cdata.get("commit") or {}).get("message") or "")).splitlines()[0][:90]
                lsha = local_git_head or str(known.get("app") or "")[:12]
                if not lsha and rsha:
                    known["app"] = rsha
                    lsha = rsha
                elif local_git_head:
                    known["app"] = local_git_head
                    lsha = local_git_head
                rel_info["local"] = lsha
                rel_info["remote"] = rsha
                rel_info["url"] = f"https://github.com/{app_repo}"
                rel_info["message"] = cmsg
                if rsha and lsha and rsha != lsha:
                    rel_info["update_available"] = True
                    has_updates = True
            except Exception as exc:
                errors.append(f"app: {exc}")
        items.insert(0, rel_info)

    result = {
        "has_updates": has_updates,
        "desktop": is_desktop_frozen(),
        "items": items,
        "errors": errors,
    }
    cfg.installed_commits_json = known
    cfg.last_checked_at = timezone.now()
    cfg.last_status = "error" if (errors and not items) else ("update_available" if has_updates else "ok")
    cfg.last_result_json = result
    cfg.save()

    if has_updates:
        updated_names = [i.get("key") for i in items if i.get("update_available")]
        _notify_staff_topic(
            topic="ops.update_available",
            title="GitHub-Updates verfügbar",
            message=f"Neue Versionen gefunden für: {', '.join(updated_names)}",
            level="info",
        )
    elif errors:
        _notify_staff_topic(
            topic="ops.update_error",
            title="Update-Prüfung fehlgeschlagen",
            message="; ".join(errors)[:400],
            level="danger",
        )

    return result


def _notify_staff_topic(topic: str, title: str, message: str, level: str = "info", link: str = "/ops/updates/"):
    try:
        from django.contrib.auth import get_user_model
        from notify_kit.models import Notification
        from notify_kit.services import send_notification
    except Exception:
        return
    User = get_user_model()
    for staff_user in User.objects.filter(is_active=True, is_staff=True):
        if Notification.objects.filter(user=staff_user, topic=topic, title=title, is_read=False).exists():
            continue
        try:
            send_notification(user=staff_user, title=title, message=message, level=level, link=link, topic=topic)
        except Exception:
            pass


def apply_nas_updates(cfg: UpdateSettings | None = None, reload_gunicorn: bool = True, user=None) -> str:
    cfg = cfg or UpdateSettings.get_solo()
    if is_desktop_frozen():
        return "Desktop-Modus (.exe): Bitte Release über den Download-Link installieren."

    import io
    import os
    buf = io.StringIO()
    base_dir = Path(getattr(settings, "BASE_DIR", "."))
    items = (cfg.last_result_json or {}).get("items") or []

    # 1. Update app repo if .git exists and update_available
    app_item = next((i for i in items if i.get("kind") == "app" and i.get("update_available")), None)
    if app_item and (base_dir / ".git").is_dir():
        _update_progress(cfg, f"▶ [1/4] Führe git pull für {app_item.get('repo', 'App')} aus...")
        try:
            gproc = subprocess.run(["git", "pull", "--ff-only"], cwd=str(base_dir), capture_output=True, text=True, timeout=60)
            gout = (gproc.stdout or "") + ("\n" + gproc.stderr if gproc.stderr else "")
            buf.write(f"App Update (git pull):\n{gout}\n")
            local_head = get_local_app_commit()
            if local_head:
                known = dict(cfg.installed_commits_json or {})
                known["app"] = local_head
                cfg.installed_commits_json = known
                app_item["local"] = local_head
                app_item["update_available"] = False
        except Exception as exc:
            buf.write(f"App Update Warnung: {exc}\n")
    else:
        _update_progress(cfg, "▶ [1/4] App-Code ist aktuell.")

    outdated = [
        i["key"]
        for i in items
        if i.get("kind") == "kit" and i.get("update_available") and i.get("key") in KIT_GITHUB_REPOS
    ]
    if outdated:
        _update_progress(cfg, f"▶ [2/4] Lade Baukasten-Kits von GitHub ({', '.join(outdated)})...")
        for k in outdated:
            repo_url = f"https://github.com/{KIT_GITHUB_REPOS[k]}/archive/refs/heads/main.zip"
            call_command(
                "design_studio_update",
                repo=repo_url,
                all_kits=False,
                reload_gunicorn=False,
                skip_migrate=(k != outdated[-1]),
                skip_collectstatic=(k != outdated[-1]),
                stdout=buf,
                stderr=buf,
            )
    else:
        _update_progress(cfg, "▶ [2/4] Keine Kit-Updates nötig (alle Baukasten-Kits aktuell).")
    out = buf.getvalue()

    # Rewire: ensure every installed kit has its URL + menu entry wired
    _update_progress(cfg, "▶ [2b/4] Prüfe URL-Verdrahtung für installierte Kits...")
    installed_apps = set(getattr(settings, "INSTALLED_APPS", []))
    for kit_key, (_, url_snippet) in KIT_METADATA.items():
        if kit_key not in installed_apps or not url_snippet:
            continue
        try:
            notes = _auto_wire_kit_in_project(kit_key, url_snippet)
            if notes:
                out += f"\nRewire {kit_key}: " + "; ".join(notes)
        except Exception as exc:
            out += f"\nRewire {kit_key} Hinweis: {exc}"

    _update_progress(cfg, "▶ [3/4] Führe Datenbank-Migrationen & Asset-Sammlung aus...")
    manage_py = base_dir / "manage.py"
    if manage_py.exists():
        has_migrate_all = (base_dir / "core" / "management" / "commands" / "migrate_all.py").exists()
        if has_migrate_all:
            try:
                mp = subprocess.run([sys.executable, str(manage_py), "migrate_all"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
                if mp.stdout:
                    out += "\n" + mp.stdout
            except Exception as exc:
                out += f"\nmigrate_all Hinweis: {exc}"

    known = dict(cfg.installed_commits_json or {})
    for item in items:
        if item.get("remote"):
            known[item["key"]] = item["remote"]
            item["local"] = item["remote"]
            item["update_available"] = False

    _update_progress(cfg, "▶ [4/4] Updates erfolgreich eingespielt! Starte Server neu...")
    cfg = UpdateSettings.get_solo()
    cfg.installed_commits_json = known
    cfg.last_status = "updated"
    cfg.last_log = f"{cfg.last_log}\n\nDetail-Protokoll:\n{out}"[-4000:]
    if isinstance(cfg.last_result_json, dict):
        cfg.last_result_json["has_updates"] = any(i.get("update_available") for i in items)
        cfg.last_result_json["items"] = items
    cfg.save()
    log_audit(user, "AUTO_UPDATE_APPLIED", target="all-kits", details=out[:500])

    if reload_gunicorn:
        restart_server_or_container()
    return out
