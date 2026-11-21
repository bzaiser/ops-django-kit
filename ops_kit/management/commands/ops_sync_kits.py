from pathlib import Path
import subprocess
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ops_kit.models import UpdateSettings
from ops_kit.runtime import get_kit_target_dir, read_kit_commits, write_kit_commits
from ops_kit.updater import _fetch_remote_head_sha, active_kit_repos


class Command(BaseCommand):
    help = "Synchronisiert alle aktiven Baukasten-Kits persistent nach /opt/ds-pkg (nur geänderte Commits)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Installiert alle aktiven Kits unabhängig vom gespeicherten Commit neu.",
        )
        parser.add_argument(
            "--target",
            default="",
            help="Optionales Zielverzeichnis (Standard: DESIGN_STUDIO_TARGET / PYTHONPATH / /opt/ds-pkg).",
        )

    def handle(self, *args, **options):
        force = bool(options.get("force"))
        target = (options.get("target") or "").strip() or get_kit_target_dir()
        base_dir = Path(getattr(settings, "BASE_DIR", "."))
        active = active_kit_repos()
        if not active:
            self.stdout.write("Keine aktiven Baukasten-Kits gefunden.")
            return

        stored = read_kit_commits(base_dir)
        updated_commits: dict[str, str] = {}
        updated_keys: list[str] = []

        for key, repo in active:
            remote_sha = _fetch_remote_head_sha(repo)
            local_sha = stored.get(key, "")
            pkg_missing_in_target = bool(target) and not (Path(target) / key).is_dir()

            needs_install = force or pkg_missing_in_target or (bool(remote_sha) and remote_sha != local_sha)
            if not needs_install:
                self.stdout.write(f"  ✓ {key}: aktuell ({local_sha or 'lokal'})")
                continue

            reason = "erzwungen" if force else ("fehlt in Zielverzeichnis" if pkg_missing_in_target else f"{local_sha or 'neu'} -> {remote_sha}")
            self.stdout.write(f"  ↓ {key} ({reason})...")
            zip_url = f"https://github.com/{repo}/archive/refs/heads/main.zip"
            cmd = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--force-reinstall",
                "--no-deps",
                "--no-cache-dir",
            ]
            if target:
                cmd.extend(["--target", target])
            cmd.append(zip_url)

            try:
                subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
            except subprocess.CalledProcessError as exc:
                err = (exc.stderr or exc.stdout or "").strip()[:400]
                raise CommandError(f"Sync für {key} fehlgeschlagen: {err}") from exc

            if remote_sha:
                updated_commits[key] = remote_sha
            updated_keys.append(key)

        if updated_commits:
            write_kit_commits(updated_commits, base_dir)
            try:
                cfg = UpdateSettings.get_solo()
                known = dict(cfg.installed_commits_json or {})
                known.update(updated_commits)
                cfg.installed_commits_json = known
                cfg.save(update_fields=["installed_commits_json"])
            except Exception:
                pass

        if updated_keys:
            dest_label = target or "site-packages"
            self.stdout.write(self.style.SUCCESS(f"Kits synchronisiert ({dest_label}): {', '.join(updated_keys)}"))
        else:
            self.stdout.write(self.style.SUCCESS("Alle aktiven Baukasten-Kits sind aktuell."))
