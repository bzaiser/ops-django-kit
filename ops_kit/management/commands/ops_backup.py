from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from ops_kit.backup_service import run_sync_backup


class Command(BaseCommand):
    help = "Erstellt ein ZIP-Backup (Datenbank + Medien) im Backup-Ordner."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default="",
            help="Optionaler Zielordner für Backup-Dateien.",
        )

    def handle(self, *args, **options):
        fp, _fname, size_mb = run_sync_backup(output_dir=options.get("output_dir") or "")
        self.stdout.write(self.style.SUCCESS(f"ZIP-Backup erstellt ({size_mb:.2f} MB): {fp}"))
