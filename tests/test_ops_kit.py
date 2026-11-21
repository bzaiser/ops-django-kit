import io
import os
from pathlib import Path
import sys
import tempfile

import django
from django.conf import settings
from django.core.management import call_command
from django.test import Client, TestCase

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DS_DIR = os.path.join(os.path.dirname(BASE_DIR), "django-design-studio")
NOTIFY_DIR = os.path.join(os.path.dirname(BASE_DIR), "notify-django-kit")
for p in (BASE_DIR, DS_DIR, NOTIFY_DIR):
    if p not in sys.path and os.path.isdir(p):
        sys.path.insert(0, p)

if not settings.configured:
    installed_apps = [
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.messages",
        "django.contrib.staticfiles",
        "design_studio",
        "ops_kit",
    ]
    if os.path.isdir(NOTIFY_DIR):
        installed_apps.append("notify_kit")
    settings.configure(
        DEBUG=True,
        SECRET_KEY="test-ops-kit",
        BASE_DIR=BASE_DIR,
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        INSTALLED_APPS=installed_apps,
        MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ],
        ROOT_URLCONF="tests.test_ops_kit",
        TEMPLATES=[{
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "APP_DIRS": True,
            "OPTIONS": {
                "context_processors": [
                    "django.template.context_processors.request",
                    "django.contrib.auth.context_processors.auth",
                    "django.contrib.messages.context_processors.messages",
                    "design_studio.context_processors.theme_context",
                ],
            },
        }],
        STATIC_URL="/static/",
    )
    django.setup()

from django.contrib.auth import get_user_model
from django.urls import include, path
from ops_kit.models import log_audit

urlpatterns = [
    path("design-studio/", include("design_studio.urls")),
    path("ops/", include("ops_kit.urls")),
]


class OpsKitTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(username="ops_admin", password="pw", is_staff=True)
        self.client = Client()

    def test_healthz_audit_log_and_backup(self):
        hz = self.client.get("/ops/healthz/")
        self.assertEqual(hz.status_code, 200)
        self.assertEqual(hz.json()["status"], "ok")
        self.assertTrue(hz.json()["db"])

        log_audit(self.staff, "BACKUP_RUN", target="nas-db")
        self.client.force_login(self.staff)
        audit_resp = self.client.get("/ops/audit/")
        self.assertEqual(audit_resp.status_code, 200)
        self.assertIn("BACKUP_RUN", audit_resp.content.decode("utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            call_command("ops_backup", output_dir=tmp, stdout=out)
            files = list(Path(tmp).glob("backup_*.json"))
            self.assertEqual(len(files), 1)

    def test_github_auto_update_settings_check_and_startup_interval(self):
        from ops_kit.models import UpdateSettings
        from ops_kit.updater import check_github_updates, should_run_startup_check

        cfg = UpdateSettings.get_solo()
        self.assertTrue(cfg.enabled)
        self.assertTrue(cfg.check_on_startup)
        self.assertTrue(cfg.auto_apply_kits)
        self.assertEqual(cfg.check_interval_hours, 24)
        self.assertTrue(should_run_startup_check(cfg))

        cfg.installed_commits_json = {"design_studio": "111111111111", "ops_kit": "222222222222"}
        cfg.save()

        def fake_fetch(url):
            if "django-design-studio" in url:
                return {"sha": "aaaaaaaaaaaa9999", "commit": {"message": "feat: new liquid"}}
            return {"sha": "222222222222", "commit": {"message": "chore: ok"}}

        res = check_github_updates(cfg, force=True, fetch_json=fake_fetch)
        self.assertTrue(res["has_updates"])
        self.assertEqual(cfg.last_status, "update_available")
        self.assertFalse(should_run_startup_check(cfg))

        out = io.StringIO()
        call_command("ops_auto_update", startup=True, stdout=out)
        self.assertIn("übersprungen", out.getvalue())

        self.client.force_login(self.staff)
        r_get = self.client.get("/ops/updates/")
        self.assertEqual(r_get.status_code, 200)
        self.assertIn("Auto-Updates (GitHub)", r_get.content.decode("utf-8"))

        r_post = self.client.post(
            "/ops/updates/",
            {
                "action": "save_config",
                "enabled": "on",
                "check_on_startup": "on",
                "auto_apply_kits": "on",
                "check_interval_hours": "12",
                "github_repo": "bzaiser/finanzplan",
            },
        )
        self.assertEqual(r_post.status_code, 302)
        cfg.refresh_from_db()
        self.assertEqual(cfg.check_interval_hours, 12)
        self.assertEqual(cfg.github_repo, "bzaiser/finanzplan")

        if "notify_kit" in settings.INSTALLED_APPS:
            from notify_kit.models import Notification
            from notify_kit.registry import get_topic
            self.assertIsNotNone(get_topic("ops.update_available"))
            self.assertEqual(Notification.objects.filter(user=self.staff, topic="ops.update_available").count(), 1)
            # Second check should not create a duplicate unread notification
            check_github_updates(cfg, force=True, fetch_json=fake_fetch)
            self.assertEqual(Notification.objects.filter(user=self.staff, topic="ops.update_available").count(), 1)

    def test_auto_wire_kit_inserts_before_root_catchall(self):
        from ops_kit.updater import _auto_wire_kit_in_project
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conf_dir = tmp_path / "config"
            conf_dir.mkdir()
            settings_file = conf_dir / "settings.py"
            settings_file.write_text("INSTALLED_APPS = [\n    'django.contrib.admin',\n]\n", encoding="utf-8")
            urls_file = conf_dir / "urls.py"
            urls_file.write_text(
                "from django.urls import path\nurlpatterns = [\n    path('admin/', admin.site.urls),\n    path('', include('core.urls')),\n]\n",
                encoding="utf-8",
            )
            with self.settings(BASE_DIR=tmp_path, ROOT_URLCONF="config.urls"):
                notes = _auto_wire_kit_in_project("cal_kit", 'path("calendar/", include("cal_kit.urls")),')
                new_urls = urls_file.read_text(encoding="utf-8")
                self.assertIn('path("calendar/", include("cal_kit.urls")),', new_urls)
                # Ensure calendar/ appears BEFORE path('',
                cal_idx = new_urls.index('path("calendar/"')
                root_idx = new_urls.index("path('',")
                self.assertLess(cal_idx, root_idx)


if __name__ == "__main__":
    from django.test.runner import DiscoverRunner
    call_command("migrate", verbosity=0)
    runner = DiscoverRunner(verbosity=2)
    sys.exit(bool(runner.run_tests(["tests.test_ops_kit"])))
