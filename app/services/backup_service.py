import shutil
from datetime import datetime
from pathlib import Path

from app.config import settings


def backup_database(dest_dir: Path | None = None) -> Path:
    dest_dir = dest_dir or settings.backup_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"newsroom-{timestamp}.sqlite3"
    shutil.copy2(settings.database_path, dest)
    prune_backups(dest_dir)
    return dest


def list_backups(dest_dir: Path | None = None) -> list[dict]:
    dest_dir = dest_dir or settings.backup_dir
    if not dest_dir.exists():
        return []
    items = sorted(dest_dir.glob("*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    return [
        {
            "name": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        }
        for path in items
    ]


def restore_database(backup_name: str, dest_dir: Path | None = None) -> Path:
    dest_dir = dest_dir or settings.backup_dir
    backup_path = (dest_dir / backup_name).resolve()
    if dest_dir.resolve() not in backup_path.parents:
        raise ValueError("Invalid backup path")
    if not backup_path.exists():
        raise FileNotFoundError(backup_name)
    safety_backup = backup_database(dest_dir)
    shutil.copy2(backup_path, settings.database_path)
    return safety_backup


def prune_backups(dest_dir: Path | None = None) -> None:
    dest_dir = dest_dir or settings.backup_dir
    backups = sorted(dest_dir.glob("*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    for backup in backups[settings.max_backup_count :]:
        backup.unlink(missing_ok=True)
