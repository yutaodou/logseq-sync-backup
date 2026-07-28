#!/usr/bin/env python3
"""
Backup a directory to Cloudflare R2 with safe SQLite snapshot + rclone sync.

Usage:
    ./backup-to-r2.py /path/to/data r2:my-bucket
    ./backup-to-r2.py /path/to/data r2:my-bucket/sub/folder

Safe SQLite backups use sqlite3's .backup command for consistent snapshots,
even for databases under active write load (WAL/journal modes).

rclone sync is used for mirroring (source == destination). If you want to
keep extra files in R2, change the mode below or use rclone copy instead.
"""

import argparse
import fcntl
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
RCLONE_LOG_FILE = "rclone-sync.log"
SCRIPT_LOG_FILE = "backup-to-r2.log"
RETENTION_DAYS = 7
LOCK_FILE = "/tmp/backup-to-r2.lock"

# Change to "copy" to keep extra files on remote
RCLONE_MODE = "sync"


def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("backup-to-r2")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


def acquire_lock() -> int:
    """Acquire an exclusive file lock to prevent concurrent runs."""
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        print(f"Another instance is already running (lock: {LOCK_FILE})")
        sys.exit(1)
    return lock_fd


def check_prerequisites(logger: logging.Logger, src_dir: Path) -> None:
    if not src_dir.is_dir():
        logger.error("Source directory does not exist: %s", src_dir)
        sys.exit(1)

    for tool in ("sqlite3", "rclone"):
        if shutil.which(tool) is None:
            logger.error("Required tool not found in PATH: %s", tool)
            sys.exit(1)

    logger.info(
        "Prerequisites OK: directory=%s, sqlite3=%s, rclone=%s",
        src_dir,
        shutil.which("sqlite3"),
        shutil.which("rclone"),
    )


_BACKUP_RE = re.compile(r"-\d{8}$")


def find_sqlite_files(src_dir: Path) -> list[Path]:
    """Recursively find all .sqlite, .sqlite3, .db files, skipping backups."""
    results: list[Path] = []
    for pattern in ("*.sqlite", "*.sqlite3", "*.db"):
        for path in src_dir.rglob(pattern):
            if not _BACKUP_RE.search(path.stem):
                results.append(path)
    return results


def backup_sqlite_file(db_path: Path, logger: logging.Logger):
    """Create a consistent backup of a single SQLite database.

    Uses sqlite3 .backup for transaction-safe snapshot (handles WAL/journal).
    Writes to a temp file first, then atomically renames to the final name.
    Skips if today's backup already exists (idempotent).
    Returns Path if successful, None on failure.
    """
    stem = db_path.stem
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    backup_name = f"{stem}-{today}.sqlite"
    backup_path = db_path.parent / backup_name

    if backup_path.exists():
        logger.info("  └─ Backup already exists for today, skipping: %s", backup_path)
        return backup_path

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=str(db_path.parent),
        prefix=f".{stem}-{today}.tmp.",
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_path_str)

    try:
        cmd = f'sqlite3 {str(db_path)!r} ".backup {str(tmp_path)!r}"'
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=300, check=False
        )
        if result.returncode != 0:
            logger.error(
                "  └─ Backup FAILED for %s: %s", db_path, result.stderr.strip()
            )
            tmp_path.unlink(missing_ok=True)
            return None

        tmp_path.rename(backup_path)
        logger.info("  └─ Backed up → %s", backup_path)
        return backup_path

    except subprocess.TimeoutExpired:
        logger.error("  └─ Backup TIMEOUT for %s (5 min)", db_path)
        tmp_path.unlink(missing_ok=True)
        return None
    except OSError as exc:
        logger.error("  └─ Backup IO ERROR for %s: %s", db_path, exc)
        tmp_path.unlink(missing_ok=True)
        return None


def cleanup_old_backups(db_path: Path, logger: logging.Logger) -> None:
    """Remove backups older than RETENTION_DAYS for this database."""
    cutoff = datetime.now(timezone.utc).timestamp() - RETENTION_DAYS * 86400
    removed = 0
    stem = db_path.stem
    for backup in db_path.parent.glob(f"{stem}-????????.sqlite"):
        try:
            if backup.stat().st_mtime < cutoff:
                backup.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info(
            "  └─ Cleaned up %d old backup(s) (>%d days)", removed, RETENTION_DAYS
        )


def run_rclone_sync(
    src_dir: Path,
    bucket: str,
    log_file: Path,
    logger: logging.Logger,
) -> int:
    """Run rclone sync (or copy) to R2 bucket, overwriting rclone log."""
    log_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "rclone",
        RCLONE_MODE,
        str(src_dir),
        bucket,
        "--fast-list",
        "--log-file",
        str(log_file),
        "--log-level",
        "INFO",
    ]

    log_file.write_text("")

    logger.info("rclone %s %s → %s", RCLONE_MODE, src_dir, bucket)
    logger.info("  rclone log: %s", log_file)

    result = subprocess.run(cmd, capture_output=False, text=True, check=False)

    if result.returncode == 0:
        logger.info("rclone sync completed successfully")
    else:
        logger.error("rclone sync FAILED with exit code %d", result.returncode)

    return result.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backup a directory to Cloudflare R2 with safe SQLite snapshots",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  ./backup-to-r2.py /data/my-project r2:my-bucket\n"
            "  ./backup-to-r2.py /data/my-project r2:my-bucket/sub/folder\n"
            "\n"
            "Crontab entry (daily at 3 AM):\n"
            "  0 3 * * * /path/to/backup-to-r2.py /data/my-project r2:my-bucket\n"
        ),
    )
    parser.add_argument(
        "src_dir",
        type=str,
        help="Path to the directory to backup",
    )
    parser.add_argument(
        "bucket",
        type=str,
        help="rclone destination (e.g. r2:my-bucket or r2:my-bucket/sub/folder)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    src_dir = Path(args.src_dir).resolve()
    bucket = args.bucket

    rclone_log = SCRIPT_DIR / RCLONE_LOG_FILE
    script_log = SCRIPT_DIR / SCRIPT_LOG_FILE

    logger = setup_logging(script_log)

    try:
        lock_fd = acquire_lock()
    except SystemExit:
        sys.exit(1)

    overall_ok = True

    try:
        logger.info("=" * 60)
        logger.info("Backup started: %s → %s", src_dir, bucket)
        logger.info("Log dir: %s", SCRIPT_DIR)

        # Step 1: prerequisites
        check_prerequisites(logger, src_dir)

        # Step 2: safe SQLite backups
        sqlite_files = find_sqlite_files(src_dir)
        logger.info("Found %d SQLite database(s)", len(sqlite_files))

        failures = 0
        for db in sqlite_files:
            relative = db.relative_to(src_dir)
            logger.info("  Processing: %s", relative)
            backup_path = backup_sqlite_file(db, logger)
            if backup_path is None:
                failures += 1
                overall_ok = False
            else:
                cleanup_old_backups(db, logger)

        if failures:
            logger.warning(
                "SQLite backup finished with %d failure(s) out of %d database(s)",
                failures,
                len(sqlite_files),
            )
        else:
            logger.info("All SQLite backups completed successfully")

        # Step 3: rclone sync
        rc = run_rclone_sync(src_dir, bucket, rclone_log, logger)
        if rc != 0:
            overall_ok = False

    except Exception:
        logger.exception("Unhandled exception during backup")
        overall_ok = False
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    if overall_ok:
        logger.info("Backup completed successfully")
    else:
        logger.error("Backup completed WITH ERRORS (see above)")
        sys.exit(1)


if __name__ == "__main__":
    main()
