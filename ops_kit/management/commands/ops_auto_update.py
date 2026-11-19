from django.core.management.base import BaseCommand

from ops_kit.models import UpdateSettings
from ops_kit.updater import (
    apply_nas_updates,
    check_github_updates,
    is_desktop_frozen,
    should_run_startup_check,
)


class Command(BaseCommand):
    help = "Prüft auf GitHub-Updates für aktive Baukasten-Kits/App und installiert sie optional automatisch."

    def add_arguments(self, parser):
        parser.add_argument("--startup", action="store_true", help="Nur prüfen, falls Intervall (z. B. 24h) abgelaufen ist.")
        parser.add_argument("--apply", action="store_true", help="Gefundene Kit-Updates direkt einspielen (nur NAS/Server).")
        parser.add_argument("--force", action="store_true", help="Prüfung unabhängig vom Intervall erzwingen.")

    def handle(self, *args, **options):
        cfg = UpdateSettings.get_solo()
        if options.get("startup") and not options.get("force"):
            if not should_run_startup_check(cfg):
                self.stdout.write("Update-Prüfung übersprungen (Intervall noch aktiv oder deaktiviert).")
                return

        res = check_github_updates(cfg, force=bool(options.get("force")))
        if res.get("has_updates"):
            self.stdout.write(self.style.WARNING("Neue GitHub-Updates verfügbar."))
            should_apply = bool(options.get("apply")) or (bool(options.get("startup")) and cfg.auto_apply_kits)
            if should_apply and not is_desktop_frozen():
                out = apply_nas_updates(cfg, reload_gunicorn=False)
                self.stdout.write(self.style.SUCCESS(f"Updates eingespielt:\n{out}"))
        else:
            self.stdout.write(self.style.SUCCESS("Alle geprüften Pakete sind aktuell."))
