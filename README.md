# ops-django-kit

Wiederverwendbares Ops-Paket (`/healthz`, `AuditLog` + UI, `manage.py ops_backup`) für Docker/NAS-Django-Apps.

## Installation
```bash
pip install --no-user --upgrade --no-cache-dir git+https://github.com/bzaiser/ops-django-kit.git@main
```

```python
INSTALLED_APPS = [..., "design_studio", "ops_kit"]
# urls.py
path("ops/", include("ops_kit.urls")),
```
