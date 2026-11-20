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


def get_optional_kits_catalog() -> list[dict]:
    installed_apps = set(getattr(settings, "INSTALLED_APPS", []))
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
    settings_mod = os.environ.get("DJANGO_SETTINGS_MODULE", "") or ""
    if settings_mod:
        settings_path = base_dir.joinpath(*settings_mod.split(".")).with_suffix(".py")
    else:
        settings_path = base_dir / "config" / "settings.py"

    root_urlconf = str(getattr(settings, "ROOT_URLCONF", "") or "")
    if root_urlconf:
        urls_path = base_dir.joinpath(*root_urlconf.split(".")).with_suffix(".py")
    else:
        urls_path = base_dir / "config" / "urls.py"

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
            m = re.search(r"(urlpatterns\s*=\s*\[[\s\S]*?)(\n\])", utxt)
            if m:
                new_utxt = utxt[:m.end(1)] + f"\n    {url_snippet}" + utxt[m.start(2):]
                urls_path.write_text(new_utxt, encoding="utf-8")
                notes.append(f"urls.py: {url_snippet} hinzugefügt.")
    return notes


def install_kit_package(kit_key: str, user=None, reload_gunicorn: bool = True) -> str:
    if kit_key not in KIT_GITHUB_REPOS:
        raise ValueError(f"Unbekanntes Kit: {kit_key}")
    if is_desktop_frozen():
        return "Desktop-Modus (.exe): Nachinstallation im Bundle nicht unterstützt."
    import os
    import signal
    repo = KIT_GITHUB_REPOS[kit_key]
    zip_url = f"https://github.com/{repo}/archive/refs/heads/main.zip"
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-deps", "--no-cache-dir"]
    target_dir = Path("/opt/ds-pkg")
    if target_dir.exists() and os.access(target_dir, os.W_OK):
        cmd.extend(["--target", str(target_dir)])
    cmd.append(zip_url)
    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180, check=True)
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")

    _, url_snippet = KIT_METADATA.get(kit_key, (kit_key, ""))
    try:
        wire_notes = _auto_wire_kit_in_project(kit_key, url_snippet)
        if wire_notes:
            out += "\n" + "\n".join(wire_notes)
    except Exception as exc:
        out += f"\nAuto-Aktivierung Warnung: {exc}"

    manage_py = Path(getattr(settings, "BASE_DIR", ".")) / "manage.py"
    if manage_py.exists():
        for mcmd in (
            [sys.executable, str(manage_py), "migrate", "--noinput"],
            [sys.executable, str(manage_py), "collectstatic", "--clear", "--noinput"],
        ):
            try:
                mp = subprocess.run(mcmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
                if mp.stdout:
                    out += "\n" + mp.stdout
                if mp.stderr:
                    out += "\n" + mp.stderr
            except Exception as exc:
                out += f"\n{mcmd[-2]} Hinweis: {exc}"

    cfg = UpdateSettings.get_solo()
    cfg.last_status = "updated"
    cfg.last_log = f"Kit {kit_key} installiert & aktiviert:\n{out}"[-4000:]
    cfg.save(update_fields=["last_status", "last_log"])
    log_audit(user, "KIT_INSTALLED", target=kit_key, details=out[:500])

    if reload_gunicorn and hasattr(signal, "SIGHUP"):
        try:
            os.kill(1, signal.SIGHUP)
        except Exception:
            pass
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
        rel_info = {"key": "app", "repo": app_repo, "kind": "app", "local": local_ver or str(known.get("app") or "")[:12], "remote": "", "message": "", "update_available": False, "url": f"https://github.com/{app_repo}/releases", "download_url": ""}
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
        except Exception:
            try:
                cdata = fetcher(f"https://api.github.com/repos/{app_repo}/commits/main")
                rsha = str(cdata.get("sha") or "")[:12]
                lsha = str(known.get("app") or "")[:12]
                if not lsha and rsha:
                    known["app"] = rsha
                    lsha = rsha
                rel_info["local"] = lsha
                rel_info["remote"] = rsha
                rel_info["message"] = str(((cdata.get("commit") or {}).get("message") or "")).splitlines()[0][:90]
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
    return result


def apply_nas_updates(cfg: UpdateSettings | None = None, reload_gunicorn: bool = True, user=None) -> str:
    cfg = cfg or UpdateSettings.get_solo()
    if is_desktop_frozen():
        return "Desktop-Modus (.exe): Bitte Release über den Download-Link installieren."

    import io
    import os
    import signal
    buf = io.StringIO()
    call_command("design_studio_update", all_kits=True, reload_gunicorn=False, stdout=buf, stderr=buf)
    out = buf.getvalue()

    known = dict(cfg.installed_commits_json or {})
    items = (cfg.last_result_json or {}).get("items") or []
    for item in items:
        if item.get("kind") == "kit" and item.get("remote"):
            known[item["key"]] = item["remote"]
            item["local"] = item["remote"]
            item["update_available"] = False

    cfg.installed_commits_json = known
    cfg.last_status = "updated"
    cfg.last_log = out[-4000:]
    if isinstance(cfg.last_result_json, dict):
        cfg.last_result_json["has_updates"] = any(i.get("update_available") for i in items)
        cfg.last_result_json["items"] = items
    cfg.save()
    log_audit(user, "AUTO_UPDATE_APPLIED", target="all-kits", details=out[:500])

    if reload_gunicorn and hasattr(signal, "SIGHUP"):
        try:
            os.kill(1, signal.SIGHUP)
        except Exception:
            pass
    return out
