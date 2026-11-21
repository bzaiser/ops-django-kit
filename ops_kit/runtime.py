"""Persistent runtime kit registry and container-wide kit commit store.

Allows dynamically installed Baukasten kits to persist across restarts,
container recreations, and `git pull` without ever modifying tracked
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
    "ops_kit": ("ops/", "ops_kit.urls"),
}

KNOWN_KITS: tuple[str, ...] = (
    "design_studio",
    "domain_kit",
    "auth_kit",
    "notify_kit",
    "cal_kit",
    "grid_kit",
    "seed_kit",
    "ops_kit",
)


def get_runtime_kits_file(base_dir) -> Path:
    return Path(base_dir) / "data" / "installed_kits.json"


def load_runtime_kits(base_dir) -> list[str]:
    path = get_runtime_kits_file(base_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("kits", [])
        if not isinstance(raw, list):
            return []
        return [str(k).strip() for k in raw if str(k).strip() in KNOWN_KITS]
    except Exception:
        return []


def add_runtime_kit(kit_key: str, base_dir) -> bool:
    if kit_key not in KNOWN_KITS:
        return False
    kits = load_runtime_kits(base_dir)
    if kit_key in kits:
        return False
    kits.append(kit_key)
    path = get_runtime_kits_file(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kits": kits}, indent=2), encoding="utf-8")
    return True


def apply_runtime_installed_apps(installed_apps: list[str], base_dir) -> list[str]:
    """Append dynamically installed kits from data/installed_kits.json if importable."""
    for kit in load_runtime_kits(base_dir):
        if kit not in installed_apps and importlib.util.find_spec(kit) is not None:
            installed_apps.append(kit)
    return installed_apps


def get_runtime_urlpatterns(base_dir, existing_patterns=None) -> list:
    """Return URL patterns for dynamically installed kits not already wired in urls.py."""
    from django.urls import include, path

    existing_repr = ""
    if existing_patterns:
        existing_repr = " ".join(str(p) for p in existing_patterns)

    extra = []
    for kit in load_runtime_kits(base_dir):
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
