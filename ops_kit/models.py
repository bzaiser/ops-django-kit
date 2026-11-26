from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ops_kit_audit_logs",
    )
    action = models.CharField(max_length=80, db_index=True)
    target = models.CharField(max_length=160, blank=True, default="")
    details = models.TextField(blank=True, default="")
    ip_address = models.CharField(max_length=45, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} ({self.target})"


def log_audit(user, action: str, target: str = "", details: str = "", request=None):
    ip = ""
    if request is not None:
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip = (xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", ""))[:45]
    return AuditLog.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        action=action[:80],
        target=target[:160],
        details=details,
        ip_address=ip,
    )


class UpdateSettings(models.Model):
    enabled = models.BooleanField(default=True)
    check_on_startup = models.BooleanField(default=True)
    auto_apply_kits = models.BooleanField(default=True)
    check_interval_hours = models.PositiveSmallIntegerField(default=24)
    github_repo = models.CharField(max_length=160, blank=True, default="")
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=32, blank=True, default="never")
    installed_commits_json = models.JSONField(default=dict, blank=True)
    last_result_json = models.JSONField(default=dict, blank=True)
    last_log = models.TextField(blank=True, default="")
    backup_frequency = models.CharField(max_length=16, default="weekly")
    backup_path = models.CharField(max_length=500, blank=True, default="")
    backup_max_keep = models.PositiveSmallIntegerField(default=10)
    backup_last_run = models.DateTimeField(null=True, blank=True)
    backup_before_update = models.BooleanField(default=True)

    @classmethod
    def get_solo(cls):
        default_repo = str(getattr(settings, "OPS_GITHUB_REPO", "") or "").strip()[:160]
        obj, created = cls.objects.using("default").get_or_create(pk=1, defaults={"github_repo": default_repo})
        if not created and not obj.github_repo and default_repo:
            obj.github_repo = default_repo
            obj.save(using="default", update_fields=["github_repo"])
        return obj

    def save(self, *args, **kwargs):
        kwargs.setdefault("using", "default")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"UpdateSettings ({self.last_status})"
