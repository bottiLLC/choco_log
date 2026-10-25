from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypedDict

_ROOT: Final[Path] = Path(__file__).resolve().parent
_DATA_DIR: Final[Path] = _ROOT / "data"
_CONFIG_FILE: Final[Path] = _DATA_DIR / "backup_config.json"
_DEFAULT_BACKUP_DIR: Final[Path] = _ROOT / "backups"


class SetBackupDirResult(TypedDict):
    """Result payload for setting backup directory."""

    success: bool
    message: str


class BackupResult(TypedDict, total=False):
    """Result payload for running data directory backup."""

    success: bool
    filename: str
    size: str
    destination: str
    timestamp: str
    message: str


def get_backup_dir() -> Path:
    """Retrieve the resolved backup destination path.

    Returns:
        Path: Target directory for backup archives.
    """
    if _CONFIG_FILE.exists():
        try:
            with open(_CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
                custom_path = data.get("backup_dir")
                if custom_path:
                    return Path(custom_path).resolve()
        except (OSError, json.JSONDecodeError):
            pass

    env_dir = os.getenv("BACKUP_DIR")
    if env_dir:
        return Path(env_dir).resolve()

    return _DEFAULT_BACKUP_DIR.resolve()


def set_backup_dir(target_dir: str) -> SetBackupDirResult:
    """Persist custom backup destination configuration.

    Args:
        target_dir: Directory path string to store backups.

    Returns:
        SetBackupDirResult: Operation status and receipt message.
    """
    path = Path(target_dir).resolve()
    # 1. Validate target destination directory
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe_file = path / ".write_probe.tmp"
        try:
            probe_file.touch(exist_ok=True)
            probe_file.unlink(missing_ok=True)
        except OSError as e:
            return {
                "success": False,
                "message": f"保存先フォルダへの書き込み権限がありません: {e!s}",
            }
    except OSError as e:
        return {
            "success": False,
            "message": f"保存先フォルダの作成に失敗しました: {e!s}",
        }

    # 2. Persist to configuration file
    try:
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"backup_dir": str(path)}, f, indent=2)
    except OSError as e:
        return {
            "success": False,
            "message": f"内部設定ファイルの書き込みに失敗しました（権限がありません）: {e!s}",
        }

    return {"success": True, "message": f"バックアップ先を設定しました: {path!s}"}


def run_backup(
    app_name: str = "app",
    source_dir: str | Path | None = None,
) -> BackupResult:
    """Execute verified atomic ZIP backup of persistent data directory.

    Args:
        app_name: Identifier prefix used for backup archive filename.
        source_dir: Directory path to backup. Defaults to standard _DATA_DIR.

    Returns:
        BackupResult: Status dictionary with archive metadata and integrity flag.
    """
    source_path = Path(source_dir).resolve() if source_dir else _DATA_DIR.resolve()

    # 1. Verify existence of source data directory
    if not source_path.exists() or not list(source_path.iterdir()):
        return {
            "success": False,
            "message": f"バックアップ対象が存在しないか空欄です: {source_path!s}",
        }

    # 2. Resolve destination path and prepare folder
    backup_dir = get_backup_dir()
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {
            "success": False,
            "message": f"保存先フォルダの作成に失敗しました: {e!s}",
            "timestamp": datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        }

    now = datetime.now(UTC).astimezone()
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    zip_filename = f"{app_name}_backup_{timestamp_str}.zip"
    final_destination = backup_dir / zip_filename

    # 3. Atomic processing in temp directory
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_zip = Path(tmp_dir) / zip_filename

        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file_path in source_path.rglob("*"):
                # Exclude backup configuration file itself from archive
                if file_path.is_file() and file_path.resolve() != _CONFIG_FILE.resolve():
                    zipf.write(file_path, file_path.relative_to(source_path))

        # 4. Integrity check (testzip)
        with zipfile.ZipFile(tmp_zip, "r") as zipf:
            corrupt = zipf.testzip()
            if corrupt is not None:
                return {
                    "success": False,
                    "message": f"整合性チェック失敗（破損検知）: {corrupt}",
                    "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                }

        # 5. Atomic move with OS-level error protection
        file_size = tmp_zip.stat().st_size
        try:
            shutil.move(str(tmp_zip), str(final_destination))
        except OSError as e:
            return {
                "success": False,
                "message": f"バックアップファイルの確定移動に失敗しました: {e!s}",
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            }

    size_str = (
        f"{file_size / (1024 * 1024):.2f} MB"
        if file_size >= 1024 * 1024
        else f"{file_size / 1024:.2f} KB"
    )

    return {
        "success": True,
        "filename": zip_filename,
        "size": size_str,
        "destination": str(final_destination),
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "message": f"バックアップ完了: {zip_filename} ({size_str})",
    }
