import glob
import io
import os
import sqlite3
import string
import threading
import uuid
import zipfile

from django.conf import settings
from django.core.management import call_command
from django.utils import timezone
from django.utils.translation import gettext as _

BACKUP_JOBS = {}
_jobs_lock = threading.Lock()


def get_default_backup_dir() -> str:
    base_dir = str(getattr(settings, "BASE_DIR", "."))
    data_dir = os.path.join(base_dir, "data")
    path = os.path.join(data_dir, "backups") if os.path.isdir(data_dir) else os.path.join(base_dir, "backups")
    os.makedirs(path, exist_ok=True)
    return os.path.abspath(path)


def resolve_backup_dir(raw_path: str = "") -> str:
    clean = (raw_path or "").strip()
    if not clean:
        return get_default_backup_dir()
    if not os.path.isabs(clean):
        raise ValueError(_(f"Der Pfad '{clean}' ist kein absoluter Pfad."))
    return os.path.abspath(os.path.expanduser(clean))


def is_same_volume_as_app(target_directory: str) -> bool:
    if not target_directory:
        return True
    try:
        norm_target = os.path.abspath(os.path.expanduser(target_directory))
        norm_app = os.path.abspath(str(getattr(settings, "BASE_DIR", ".")))
        if os.name == "nt":
            d_t, _ = os.path.splitdrive(norm_target)
            d_a, _ = os.path.splitdrive(norm_app)
            return d_t.upper() == d_a.upper()
        return os.stat(norm_target).st_dev == os.stat(norm_app).st_dev
    except Exception:
        return False


def test_directory_writable(path: str) -> tuple[bool, str]:
    if not path or not path.strip():
        return False, _("Kein Pfad angegeben.")
    clean_path = path.strip()
    if not os.path.isabs(clean_path):
        return False, _("Bitte einen absoluten Pfad angeben (z. B. D:\\Backups oder /backups).")
    try:
        os.makedirs(clean_path, exist_ok=True)
        test_file = os.path.join(clean_path, f".perm_test_{uuid.uuid4().hex[:6]}")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("write_check")
        os.remove(test_file)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def is_running_in_docker() -> bool:
    return os.environ.get("RUNNING_IN_DOCKER") == "1" or os.path.exists("/.dockerenv")


def get_real_accessible_shares() -> list[dict]:
    shares = []
    in_docker = is_running_in_docker()
    if in_docker:
        if os.path.isdir("/backups"):
            writable, _err = test_directory_writable("/backups")
            if writable:
                is_separate = True
                try:
                    if os.path.abspath("/backups") == os.path.abspath("/app/data/backups"):
                        is_separate = False
                except Exception:
                    pass
                if is_separate:
                    shares.append({
                        "label": _("Externer NAS-Backup-Mount (/backups)"),
                        "path": "/backups",
                        "is_other_volume": True,
                    })
    elif os.name == "nt":
        app_drive = os.path.splitdrive(os.path.abspath(str(getattr(settings, "BASE_DIR", "."))))[0].upper()
        for letter in string.ascii_uppercase:
            drive_path = f"{letter}:\\"
            if os.path.exists(drive_path):
                writable, _err = test_directory_writable(drive_path)
                if writable:
                    is_other = letter != app_drive.rstrip(":")
                    shares.append({
                        "label": f"{letter}: Laufwerk ({_('Getrenntes Volume') if is_other else _('App-Laufwerk')})",
                        "path": os.path.join(drive_path, "Backups", "App"),
                        "is_other_volume": is_other,
                    })
    else:
        for base in ("/volume1", "/volume2", "/volume3", "/volume4", "/volumeUSB1", "/share", "/mnt", "/media", "/backup", "/backups"):
            if os.path.isdir(base):
                writable, _err = test_directory_writable(base)
                if writable:
                    shares.append({
                        "label": f"{base} ({_('Mountpoint')})",
                        "path": os.path.join(base, "app_backup"),
                        "is_other_volume": True,
                    })
    if not in_docker:
        shares.append({
            "label": _("Standard App-Verzeichnis (data/backups)"),
            "path": get_default_backup_dir(),
            "is_other_volume": False,
        })
    return shares


def get_job_status(job_id: str) -> dict | None:
    with _jobs_lock:
        return BACKUP_JOBS.get(job_id)


def _update_job_status(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in BACKUP_JOBS:
            BACKUP_JOBS[job_id].update(kwargs)


def list_backups(raw_path: str = "") -> list[dict]:
    try:
        target_dir = resolve_backup_dir(raw_path)
    except Exception:
        target_dir = get_default_backup_dir()
    if not os.path.isdir(target_dir):
        return []
    files = sorted(
        glob.glob(os.path.join(target_dir, "app_backup_*.zip")) + glob.glob(os.path.join(target_dir, "backup_*.json")),
        key=os.path.getmtime,
        reverse=True,
    )
    rows = []
    for fp in files:
        try:
            st = os.stat(fp)
            rows.append({
                "name": os.path.basename(fp),
                "size_mb": st.st_size / (1024 * 1024),
                "mtime": timezone.datetime.fromtimestamp(st.st_mtime, tz=timezone.get_current_timezone()),
            })
        except Exception:
            continue
    return rows


def _create_backup_archive(norm_target_dir: str, max_keep: int, job_id: str | None = None) -> tuple[str, str, float]:
    writable, err_msg = test_directory_writable(norm_target_dir)
    if not writable:
        raise PermissionError(_(f"Keine Schreibrechte im Ordner '{norm_target_dir}': {err_msg}"))

    timestamp_str = timezone.now().strftime("%Y%m%d_%H%M%S")
    target_filename = f"app_backup_{timestamp_str}.zip"
    target_filepath = os.path.join(norm_target_dir, target_filename)

    db_cfg = getattr(settings, "DATABASES", {}).get("default", {})
    db_engine = str(db_cfg.get("ENGINE", ""))
    db_path = str(db_cfg.get("NAME", ""))
    media_dir = str(getattr(settings, "MEDIA_ROOT", "") or os.path.join(str(getattr(settings, "BASE_DIR", ".")), "data", "media"))

    media_files = []
    if media_dir and os.path.isdir(media_dir):
        for root, _dirs, files in os.walk(media_dir):
            for fname in files:
                full_p = os.path.join(root, fname)
                rel_p = os.path.relpath(full_p, media_dir)
                media_files.append((full_p, os.path.join("media", rel_p)))

    if job_id:
        _update_job_status(job_id, progress=15, step_label=_("Erstelle konsistentes Datenbank-Abbild..."))

    with zipfile.ZipFile(target_filepath, "w", zipfile.ZIP_DEFLATED) as zf:
        if db_engine.endswith("sqlite3") and db_path and db_path != ":memory:" and os.path.isfile(db_path):
            try:
                src_conn = sqlite3.connect(db_path)
                mem_conn = sqlite3.connect(":memory:")
                src_conn.backup(mem_conn)
                src_conn.close()
                if hasattr(mem_conn, "serialize"):
                    dump = mem_conn.serialize()
                    zf.writestr("database/db.sqlite3", dump)
                else:
                    with open(db_path, "rb") as f:
                        zf.writestr("database/db.sqlite3", f.read())
                mem_conn.close()
            except Exception:
                with open(db_path, "rb") as f:
                    zf.writestr("database/db.sqlite3", f.read())
        else:
            buf = io.StringIO()
            call_command("dumpdata", exclude=["contenttypes", "auth.permission", "sessions"], indent=2, stdout=buf)
            zf.writestr("database/dumpdata.json", buf.getvalue().encode("utf-8"))

        if job_id:
            _update_job_status(job_id, progress=25, step_label=_("Datenbank gesichert. Komprimiere Medien & Dokumente..."))

        total_media = len(media_files)
        for idx, (full_p, zip_p) in enumerate(media_files, start=1):
            try:
                zf.write(full_p, zip_p)
            except Exception:
                pass
            if job_id and (idx % 5 == 0 or idx == total_media):
                pct = 25 + int((idx / max(total_media, 1)) * 65)
                _update_job_status(job_id, progress=min(pct, 90), step_label=_(f"Komprimiere Medien ({idx} / {total_media})..."))

    if job_id:
        _update_job_status(job_id, progress=95, step_label=_("Bereinige ältere Sicherungen..."))

    try:
        existing = sorted(glob.glob(os.path.join(norm_target_dir, "app_backup_*.zip")), key=os.path.getmtime, reverse=True)
        keep_n = max(1, int(max_keep or 10))
        for old_file in existing[keep_n:]:
            try:
                os.remove(old_file)
            except Exception:
                pass
    except Exception:
        pass

    size_mb = os.path.getsize(target_filepath) / (1024 * 1024)
    return target_filepath, target_filename, size_mb


def run_sync_backup(cfg=None, output_dir: str = "") -> tuple[str, str, float]:
    from .models import UpdateSettings

    if cfg is None:
        cfg = UpdateSettings.get_solo()
    target_dir = resolve_backup_dir(output_dir or cfg.backup_path)
    fp, fname, size_mb = _create_backup_archive(target_dir, cfg.backup_max_keep)
    cfg.backup_last_run = timezone.now()
    cfg.save(update_fields=["backup_last_run"])
    return fp, fname, size_mb


def _backup_worker_task(job_id: str, target_dir: str, max_keep: int):
    from django.db import connections
    from .models import UpdateSettings

    try:
        _update_job_status(job_id, status="running", progress=5, step_label=_("Bereite Datensicherung vor..."))
        fp, fname, size_mb = _create_backup_archive(target_dir, max_keep, job_id=job_id)
        cfg = UpdateSettings.get_solo()
        cfg.backup_last_run = timezone.now()
        cfg.save(update_fields=["backup_last_run"])
        _update_job_status(
            job_id,
            status="completed",
            progress=100,
            step_label=_(f"Sicherung erfolgreich erstellt ({size_mb:.2f} MB)!"),
            target_file=fp,
            filename=fname,
            target_directory=target_dir,
            file_size_mb=size_mb,
        )
    except Exception as exc:
        _update_job_status(job_id, status="failed", error=str(exc), step_label=_(f"Fehler bei Sicherung: {exc}"))
    finally:
        connections.close_all()


def start_background_backup(cfg=None) -> tuple[str, str]:
    from .models import UpdateSettings

    if cfg is None:
        cfg = UpdateSettings.get_solo()
    target_path = resolve_backup_dir(cfg.backup_path)
    writable, err_msg = test_directory_writable(target_path)
    if not writable:
        raise PermissionError(_(f"Keine Schreibrechte im Zielordner '{target_path}': {err_msg}"))

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        BACKUP_JOBS[job_id] = {
            "id": job_id,
            "status": "pending",
            "progress": 0,
            "step_label": _("Initialisiere Datensicherung..."),
            "target_file": None,
            "target_directory": target_path,
            "error": None,
            "created_at": timezone.now(),
        }
    t = threading.Thread(target=_backup_worker_task, args=(job_id, target_path, cfg.backup_max_keep), daemon=True)
    t.start()
    return job_id, target_path


def is_backup_due(cfg) -> bool:
    if not cfg or cfg.backup_frequency == "never":
        return False
    if not cfg.backup_last_run:
        return True
    days = (timezone.now() - cfg.backup_last_run).days
    if cfg.backup_frequency == "daily":
        return days >= 1
    if cfg.backup_frequency == "weekly":
        return days >= 7
    if cfg.backup_frequency == "monthly":
        return days >= 30
    return False
