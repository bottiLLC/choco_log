from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from backup_manager import get_backup_dir, run_backup, set_backup_dir


def test_run_backup_empty_source(temp_data_dir: Path) -> None:
    """Verify backup interlock halts on empty directory."""
    # Ensure source dir is empty
    result = run_backup(app_name="test_app", source_dir=str(temp_data_dir))
    assert result["success"] is False
    assert "存在しないか空欄" in str(result["message"])


def test_run_backup_success(temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify atomic ZIP creation and testzip integrity verification."""
    # Create sample persistent file
    sample_file = temp_data_dir / "sample.db"
    sample_file.write_text("dummy database content", encoding="utf-8")

    backup_dest = temp_data_dir.parent / "backup_store"
    monkeypatch.setenv("BACKUP_DIR", str(backup_dest))

    result = run_backup(app_name="test_app", source_dir=str(temp_data_dir))
    assert result["success"] is True
    assert "バックアップ完了" in str(result["message"])

    destination_path = Path(str(result["destination"]))
    assert destination_path.exists()
    assert destination_path.name.startswith("test_app_backup_")
    assert destination_path.suffix == ".zip"

    # Verify ZIP structure
    with zipfile.ZipFile(destination_path, "r") as zipf:
        assert zipf.testzip() is None
        assert "sample.db" in zipf.namelist()


def test_set_and_get_backup_dir(temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify config persistence in backup_config.json."""
    config_file = temp_data_dir / "backup_config.json"
    monkeypatch.setattr("backup_manager._CONFIG_FILE", config_file)

    target_dir = temp_data_dir / "custom_dest"
    res = set_backup_dir(str(target_dir))
    assert res["success"] is True

    resolved = get_backup_dir()
    assert resolved == target_dir.resolve()


def test_set_backup_dir_config_permission_error(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify informative error message when _CONFIG_FILE cannot be written."""
    config_file = temp_data_dir / "backup_config.json"
    monkeypatch.setattr("backup_manager._CONFIG_FILE", config_file)

    def mock_open(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("builtins.open", mock_open)
    target_dir = temp_data_dir / "custom_dest"
    res = set_backup_dir(str(target_dir))
    assert res["success"] is False
    assert "内部設定ファイル" in str(res["message"])
    assert "権限がありません" in str(res["message"])


def test_set_backup_dir_target_unwritable(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify clear error message when target backup folder is unwritable."""

    def mock_touch(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "touch", mock_touch)
    target_dir = temp_data_dir / "custom_dest"
    res = set_backup_dir(str(target_dir))
    assert res["success"] is False
    assert "保存先フォルダへの書き込み権限がありません" in str(res["message"])
