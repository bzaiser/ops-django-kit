from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ops_kit", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="UpdateSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("enabled", models.BooleanField(default=True)),
                ("check_on_startup", models.BooleanField(default=True)),
                ("auto_apply_kits", models.BooleanField(default=True)),
                ("check_interval_hours", models.PositiveSmallIntegerField(default=24)),
                ("github_repo", models.CharField(blank=True, default="", max_length=160)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("last_status", models.CharField(blank=True, default="never", max_length=32)),
                ("installed_commits_json", models.JSONField(blank=True, default=dict)),
                ("last_result_json", models.JSONField(blank=True, default=dict)),
                ("last_log", models.TextField(blank=True, default="")),
            ],
        ),
    ]
