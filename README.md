# ops-django-kit

Wiederverwendbares Ops-, Auto-Update- & Kit-Management-Paket für Django-Baukasten-Apps auf Docker/NAS.

## Features
- **Healthcheck & Audit**: `/ops/healthz/` (DB-Status) & `/ops/audit/` (`AuditLog` + UI).
- **Backups**: `python manage.py ops_backup`.
- **GitHub Auto-Updates (`/ops/updates/`)**:
  - Prüft aktive Baukasten-Kits & App-Repo via `git ls-remote` (mit GitHub-REST-API-Fallback).
  - Hintergrund-Updates mit Live-Fortschritt (`/ops/status.json`) und automatischem Server-/Container-Neustart.
  - Multi-Tenant-sicher: `UpdateSettings` liegt immer auf `using("default")`; installierte Kit-SHAs liegen container-weit in `/opt/ds-pkg/.kit_commits.json`; erkennt `migrate_all` automatisch.
- **Persistentes Kit-Management (`ops_kit.runtime`)**:
  - Nachinstallieren, Aktivieren und **Entfernen/Deaktivieren** optionaler Kits über `data/installed_kits.json` (`kits` / `disabled`) — ohne getrackte `settings.py` oder `urls.py` zu verändern!
  - Schutz für Kern-Kits (`design_studio`, `ops_kit`) sowie App-Pflicht-Kits via `OPS_REQUIRED_KITS = [...]`.
  - Beim Entfernen bleiben DB-Daten standardmäßig erhalten; optional per Checkbox `migrate <kit> zero` (inkl. aller Mandanten).
  - `domain_kit`-Teilbereiche direkt per Checkbox im Updates-Tab steuerbar.
- **CLI Kit-Sync**:
  - `python manage.py ops_sync_kits [--force]` — vergleicht alle aktiven Kits gegen `/opt/ds-pkg/.kit_commits.json` und installiert nur geänderte Kits persistent nach `/opt/ds-pkg`.

## Einbindung
```python
# settings.py
INSTALLED_APPS = [..., "design_studio", "ops_kit"]
OPS_GITHUB_REPO = "bzaiser/meine-app"
OPS_REQUIRED_KITS = ["design_studio", "ops_kit"]  # optional: weitere Pflicht-Kits schützen

try:
    from ops_kit.runtime import apply_runtime_installed_apps
    apply_runtime_installed_apps(INSTALLED_APPS, BASE_DIR, protected=OPS_REQUIRED_KITS)
except ImportError:
    pass

# urls.py
urlpatterns = [
    path("ops/", include("ops_kit.urls")),
]
try:
    from ops_kit.runtime import get_runtime_urlpatterns, optional_kit_path
    urlpatterns += optional_kit_path("domain_kit", "domain/", "domain_kit.urls")
    urlpatterns += optional_kit_path("cal_kit", "calendar/", "cal_kit.urls")
    urlpatterns += get_runtime_urlpatterns(settings.BASE_DIR, urlpatterns)
except ImportError:
    pass
```

## Docker-Persistenz (Pflicht)
```yaml
services:
  web:
    environment:
      - PYTHONPATH=/opt/ds-pkg
      - DESIGN_STUDIO_TARGET=/opt/ds-pkg
    volumes:
      - .:/app
      - ./data:/app/data
      - ds_pkg:/opt/ds-pkg

volumes:
  ds_pkg:
```
