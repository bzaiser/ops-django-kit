"""Persistent runtime kit registry and container-wide kit commit store.

Allows dynamically installed or deactivated Baukasten kits to persist across
restarts, container recreations, and `git pull` without ever modifying tracked
`settings.py` or `urls.py` files.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

KIT_URL_ROUTES: dict[str, tuple[str, str]] = {
    "domain_kit": ("domain/", "domain_kit.urls"),
    "auth_kit": ("accounts/", "auth_kit.urls"),
    "notify_kit": ("notifications/", "notify_kit.urls"),
    "cal_kit": ("calendar/", "cal_kit.urls"),
    "finance_kit": ("finance/", "finance_kit.urls"),
    "list_kit": ("lists/", "list_kit.urls"),
    "site_kit": ("", "site_kit.urls"),
    "ops_kit": ("ops/", "ops_kit.urls"),
}

KNOWN_KITS: tuple[str, ...] = (
    "design_studio",
    "domain_kit",
    "auth_kit",
    "notify_kit",
    "cal_kit",
    "list_kit",
    "site_kit",
    "finance_kit",
    "grid_kit",
    "seed_kit",
    "ops_kit",
)

CORE_PROTECTED_KITS: frozenset[str] = frozenset({"design_studio", "ops_kit"})


def get_runtime_kits_file(base_dir) -> Path:
    return Path(base_dir) / "data" / "installed_kits.json"


def _read_registry(base_dir) -> tuple[list[str], list[str]]:
    path = get_runtime_kits_file(base_dir)
    if not path.exists():
        return [], []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            kits = [str(k).strip() for k in raw if str(k).strip() in KNOWN_KITS]
            return kits, []
        if isinstance(raw, dict):
            kits = [str(k).strip() for k in (raw.get("kits") or []) if str(k).strip() in KNOWN_KITS]
            disabled = [
                str(k).strip()
                for k in (raw.get("disabled") or [])
                if str(k).strip() in KNOWN_KITS and str(k).strip() not in CORE_PROTECTED_KITS
            ]
            return kits, disabled
    except Exception:
        pass
    return [], []


def _write_registry(base_dir, kits: list[str], disabled: list[str]) -> None:
    path = get_runtime_kits_file(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kits": sorted(set(k for k in kits if k in KNOWN_KITS)),
        "disabled": sorted(set(k for k in disabled if k in KNOWN_KITS and k not in CORE_PROTECTED_KITS)),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_runtime_kits(base_dir) -> list[str]:
    kits, _ = _read_registry(base_dir)
    return kits


def load_disabled_kits(base_dir) -> list[str]:
    _, disabled = _read_registry(base_dir)
    return disabled


def add_runtime_kit(kit_key: str, base_dir) -> bool:
    if kit_key not in KNOWN_KITS:
        return False
    kits, disabled = _read_registry(base_dir)
    changed = False
    if kit_key in disabled:
        disabled = [k for k in disabled if k != kit_key]
        changed = True
    if kit_key not in kits:
        kits.append(kit_key)
        changed = True
    if changed:
        _write_registry(base_dir, kits, disabled)
    return changed


def disable_runtime_kit(kit_key: str, base_dir, protected=None) -> bool:
    """Persistently disable/remove an optional kit via data/installed_kits.json."""
    prot = set(CORE_PROTECTED_KITS) | set(protected or ())
    if kit_key not in KNOWN_KITS or kit_key in prot:
        return False
    kits, disabled = _read_registry(base_dir)
    changed = False
    if kit_key in kits:
        kits = [k for k in kits if k != kit_key]
        changed = True
    if kit_key not in disabled:
        disabled.append(kit_key)
        changed = True
    if changed:
        _write_registry(base_dir, kits, disabled)
    return changed


def apply_runtime_installed_apps(installed_apps: list[str], base_dir, protected=None) -> list[str]:
    """Apply persistent runtime kit additions and removals to INSTALLED_APPS."""
    prot = set(CORE_PROTECTED_KITS) | set(protected or ())
    kits, disabled = _read_registry(base_dir)
    disabled_set = set(disabled) - prot
    if disabled_set:
        installed_apps[:] = [app for app in installed_apps if app not in disabled_set]
    for kit in kits:
        if kit not in disabled_set and kit not in installed_apps and importlib.util.find_spec(kit) is not None:
            installed_apps.append(kit)
    return installed_apps


def filter_runtime_context_processors(context_processors: list[str], installed_apps) -> list[str]:
    """Drop context processors belonging to disabled kits so template loading never crashes."""
    active = set(installed_apps)
    filtered = []
    for cp in context_processors:
        top = str(cp).split(".", 1)[0]
        if top in KNOWN_KITS and top not in active:
            continue
        filtered.append(cp)
    return filtered


def optional_kit_path(kit_key: str, prefix: str, urls_module: str) -> list:
    """Safely include a kit's URLs only when the kit is active in settings.INSTALLED_APPS."""
    from django.conf import settings
    from django.urls import include, path

    if kit_key not in set(getattr(settings, "INSTALLED_APPS", [])):
        return []
    if importlib.util.find_spec(kit_key) is None:
        return []
    return [path(prefix, include(urls_module))]


def get_runtime_urlpatterns(base_dir, existing_patterns=None) -> list:
    """Return URL patterns for dynamically installed kits not already wired in urls.py."""
    from django.conf import settings
    from django.urls import include, path

    active_apps = set(getattr(settings, "INSTALLED_APPS", []))
    existing_repr = ""
    if existing_patterns:
        existing_repr = " ".join(str(p) for p in existing_patterns)

    extra = []
    kits, disabled = _read_registry(base_dir)
    disabled_set = set(disabled)
    for kit in kits:
        if kit in disabled_set or kit not in active_apps:
            continue
        route = KIT_URL_ROUTES.get(kit)
        if not route:
            continue
        prefix, urls_mod = route
        if urls_mod in existing_repr or f"'{prefix}'" in existing_repr or f'"{prefix}"' in existing_repr:
            continue
        if importlib.util.find_spec(kit) is None:
            continue
        extra.append(path(prefix, include(urls_mod)))
    return extra


def get_kit_target_dir() -> str:
    """Return the single persistent target directory for kit packages (usually /opt/ds-pkg)."""
    env_target = (os.environ.get("DESIGN_STUDIO_TARGET") or "").strip()
    candidates = []
    if env_target:
        candidates.append(env_target)
    for p in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        p = p.strip()
        if p and p not in candidates:
            candidates.append(p)
    if "/opt/ds-pkg" not in candidates:
        candidates.append("/opt/ds-pkg")

    for cand in candidates:
        if os.path.isdir(cand) and os.access(cand, os.W_OK):
            return cand
    return ""


def get_kit_commits_file(base_dir=None) -> Path:
    target = get_kit_target_dir()
    if target:
        return Path(target) / ".kit_commits.json"
    if base_dir is not None:
        return Path(base_dir) / "data" / ".kit_commits.json"
    return Path("data") / ".kit_commits.json"


def read_kit_commits(base_dir=None) -> dict[str, str]:
    path = get_kit_commits_file(base_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_kit_commits(commits: dict[str, str], base_dir=None) -> None:
    path = get_kit_commits_file(base_dir)
    try:
        merged = read_kit_commits(base_dir)
        merged.update({str(k): str(v) for k, v in (commits or {}).items() if v})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def remove_kit_commit(kit_key: str, base_dir=None) -> None:
    path = get_kit_commits_file(base_dir)
    try:
        data = read_kit_commits(base_dir)
        if kit_key in data:
            data.pop(kit_key, None)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass
