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

        status_resp = self.client.get("/ops/status.json")
        self.assertEqual(status_resp.status_code, 200)
        self.assertIn("status", status_resp.json())

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

    def test_runtime_kits_persistence_and_no_settings_mutation(self):
        from ops_kit.runtime import (
            add_runtime_kit,
            apply_runtime_installed_apps,
            load_runtime_kits,
            read_kit_commits,
            write_kit_commits,
        )
        from ops_kit.updater import _auto_wire_kit_in_project

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conf_dir = tmp_path / "config"
            conf_dir.mkdir()
            orig_settings = (
                "INSTALLED_APPS = ['design_studio', 'ops_kit']\n"
                "from ops_kit.runtime import apply_runtime_installed_apps\n"
                "apply_runtime_installed_apps(INSTALLED_APPS, BASE_DIR)\n"
            )
            settings_file = conf_dir / "settings.py"
            settings_file.write_text(orig_settings, encoding="utf-8")

            with self.settings(BASE_DIR=tmp_path, ROOT_URLCONF="config.urls"):
                notes = _auto_wire_kit_in_project("cal_kit", 'path("calendar/", include("cal_kit.urls")),')
                # settings.py must remain untouched when runtime hook is present
                self.assertEqual(settings_file.read_text(encoding="utf-8"), orig_settings)
                self.assertIn("cal_kit", load_runtime_kits(tmp_path))
                self.assertTrue(any("installed_kits.json" in n for n in notes))

                # apply_runtime_installed_apps only adds importable kits
                apps = ["design_studio"]
                add_runtime_kit("ops_kit", tmp_path)
                apply_runtime_installed_apps(apps, tmp_path)
                self.assertIn("ops_kit", apps)

                # Container-wide kit commits store
                pkg_dir = tmp_path / "ds_pkg"
                pkg_dir.mkdir()
                old_target = os.environ.get("DESIGN_STUDIO_TARGET")
                os.environ["DESIGN_STUDIO_TARGET"] = str(pkg_dir)
                try:
                    write_kit_commits({"ops_kit": "abc123456789"}, tmp_path)
                    self.assertEqual(read_kit_commits(tmp_path).get("ops_kit"), "abc123456789")
                finally:
                    if old_target is None:
                        os.environ.pop("DESIGN_STUDIO_TARGET", None)
                    else:
                        os.environ["DESIGN_STUDIO_TARGET"] = old_target

    def test_ops_sync_kits_skips_current_and_syncs_changed(self):
        from unittest.mock import patch
        from ops_kit.runtime import read_kit_commits, write_kit_commits

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pkg_dir = tmp_path / "ds_pkg"
            (pkg_dir / "ops_kit").mkdir(parents=True)
            (pkg_dir / "design_studio").mkdir(parents=True)
            old_target = os.environ.get("DESIGN_STUDIO_TARGET")
            os.environ["DESIGN_STUDIO_TARGET"] = str(pkg_dir)
            try:
                write_kit_commits({"ops_kit": "111111111111", "design_studio": "222222222222"}, tmp_path)
                calls = []

                def fake_sha(repo):
                    return "999999999999" if "design-studio" in repo else "111111111111"

                def fake_run(cmd, **kwargs):
                    calls.append(cmd)
                    class R:
                        returncode = 0
                        stdout = "ok"
                        stderr = ""
                    return R()

                out = io.StringIO()
                with self.settings(BASE_DIR=tmp_path), \
                     patch("ops_kit.management.commands.ops_sync_kits._fetch_remote_head_sha", side_effect=fake_sha), \
                     patch("ops_kit.management.commands.ops_sync_kits.active_kit_repos", return_value=[("design_studio", "bzaiser/django-design-studio"), ("ops_kit", "bzaiser/ops-django-kit")]), \
                     patch("ops_kit.management.commands.ops_sync_kits.subprocess.run", side_effect=fake_run):
                    call_command("ops_sync_kits", stdout=out)

                self.assertEqual(len(calls), 1)
                self.assertIn("--target", calls[0])
                self.assertIn(str(pkg_dir), calls[0])
                self.assertIn("django-design-studio", calls[0][-1])
                self.assertEqual(read_kit_commits(tmp_path).get("design_studio"), "999999999999")
            finally:
                if old_target is None:
                    os.environ.pop("DESIGN_STUDIO_TARGET", None)
                else:
                    os.environ["DESIGN_STUDIO_TARGET"] = old_target

    def test_remove_and_reactivate_kit_with_protection(self):
        from ops_kit.runtime import (
            add_runtime_kit,
            apply_runtime_installed_apps,
            disable_runtime_kit,
            filter_runtime_context_processors,
            load_disabled_kits,
            optional_kit_path,
        )
        from ops_kit.updater import get_optional_kits_catalog, remove_kit_package

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Protected kits cannot be disabled
            self.assertFalse(disable_runtime_kit("design_studio", tmp_path))
            self.assertFalse(disable_runtime_kit("ops_kit", tmp_path))
            self.assertFalse(disable_runtime_kit("grid_kit", tmp_path, protected={"grid_kit"}))

            # Optional kit can be disabled and disappears from INSTALLED_APPS
            self.assertTrue(disable_runtime_kit("cal_kit", tmp_path))
            self.assertIn("cal_kit", load_disabled_kits(tmp_path))
            apps = ["design_studio", "ops_kit", "cal_kit", "domain_kit"]
            apply_runtime_installed_apps(apps, tmp_path)
            self.assertNotIn("cal_kit", apps)
            self.assertIn("domain_kit", apps)

            # Context processors for disabled kits are filtered out
            cps = [
                "design_studio.context_processors.theme_context",
                "domain_kit.context_processors.domain_context",
                "notify_kit.context_processors.notify_context",
            ]
            filtered = filter_runtime_context_processors(cps, apps)
            self.assertIn("domain_kit.context_processors.domain_context", filtered)
            self.assertNotIn("notify_kit.context_processors.notify_context", filtered)

            # optional_kit_path returns [] for inactive kit and [path] for active kit
            self.assertEqual(optional_kit_path("cal_kit", "calendar/", "cal_kit.urls"), [])
            self.assertEqual(len(optional_kit_path("ops_kit", "ops/", "ops_kit.urls")), 1)

            # remove_kit_package marks kit disabled and shows it as was_disabled in catalog
            with self.settings(BASE_DIR=tmp_path):
                with self.assertRaises(ValueError):
                    remove_kit_package("ops_kit", reload_gunicorn=False)
                remove_kit_package("domain_kit", purge_db=False, user=self.staff, reload_gunicorn=False)
                self.assertIn("domain_kit", load_disabled_kits(tmp_path))
                catalog = {c["key"]: c for c in get_optional_kits_catalog()}
                self.assertTrue(catalog["domain_kit"]["was_disabled"])

                # Reactivating clears disabled state
                add_runtime_kit("domain_kit", tmp_path)
                self.assertNotIn("domain_kit", load_disabled_kits(tmp_path))

    def test_no_missing_migrations(self):
        call_command("makemigrations", "ops_kit", "--check", "--dry-run", verbosity=0)


if __name__ == "__main__":
    from django.test.runner import DiscoverRunner
    call_command("migrate", verbosity=0)
    runner = DiscoverRunner(verbosity=2)
    sys.exit(bool(runner.run_tests(["tests.test_ops_kit"])))
