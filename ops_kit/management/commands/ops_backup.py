from datetime import datetime
from pathlib import Path
import shutil

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Erstellt ein JSON-Backup (und optional eine SQLite-Kopie) im Backup-Ordner."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default=str(Path(getattr(settings, "BASE_DIR", ".")) / "backups"),
            help="Zielordner für Backup-Dateien.",
        )

    def handle(self, *args, **options):
        out_dir = Path(options["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        json_path = out_dir / f"backup_{stamp}.json"
        with json_path.open("w", encoding="utf-8") as fh:
            call_command("dumpdata", exclude=["contenttypes", "auth.permission", "sessions"], indent=2, stdout=fh)
        self.stdout.write(self.style.SUCCESS(f"JSON-Backup erstellt: {json_path}"))

        db_cfg = getattr(settings, "DATABASES", {}).get("default", {})
        if db_cfg.get("ENGINE", "").endswith("sqlite3"):
            db_name = str(db_cfg.get("NAME", ""))
            if db_name and db_name != ":memory:" and Path(db_name).exists():
                sqlite_path = out_dir / f"db_{stamp}.sqlite3"
                shutil.copy2(db_name, sqlite_path)
                self.stdout.write(self.style.SUCCESS(f"SQLite-Backup erstellt: {sqlite_path}"))
