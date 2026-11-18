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
