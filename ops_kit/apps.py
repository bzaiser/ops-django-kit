from django.apps import AppConfig


class OpsKitConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ops_kit"
    verbose_name = "Ops, Health & Audit Kit"

    def ready(self):
        try:
            from notify_kit.registry import register_notification_topic
        except Exception:
            return
        register_notification_topic(
            key="ops.update_available",
            label="GitHub-Updates verfügbar",
            description="Informiert Administratoren, sobald neue Updates auf GitHub gefunden wurden.",
            icon="bi-cloud-arrow-down",
            default_enabled=True,
            staff_only=True,
        )
        register_notification_topic(
            key="ops.update_error",
            label="Update fehlgeschlagen",
            description="Informiert Administratoren über Fehler bei der Update-Prüfung oder Installation.",
            icon="bi-exclamation-triangle",
            default_enabled=True,
            staff_only=True,
        )
