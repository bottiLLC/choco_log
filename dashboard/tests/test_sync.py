from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sync import (
    SyncError,
    build_csv_url,
    fetch_csv_content,
    parse_and_clean_csv,
    persist_dataframe_to_sqlite,
    sync_spreadsheet_to_sqlite,
)


def test_build_csv_url() -> None:
    """Verify correct URL structure with stripped ID."""
    url = build_csv_url("  test_sheet_id_123  ", sheet_name="Log")
    expected = (
        "https://docs.google.com/spreadsheets/d/test_sheet_id_123/gviz/tq?tqx=out:csv&sheet=Log"
    )
    assert url == expected


def test_parse_and_clean_csv_success(mock_csv_content: str) -> None:
    """Verify standard CSV parsing and calculated volume calculation."""
    df = parse_and_clean_csv(mock_csv_content)
    assert len(df) == 4
    assert list(df.columns) == [
        "timestamp",
        "name",
        "load",
        "volume",
        "mode",
        "calculated_volume",
    ]
    # Check chest press calculated volume (40 * 10 = 400.0)
    assert df.iloc[0]["calculated_volume"] == 400.0
    assert df.iloc[2]["mode"] == ""


def test_parse_and_clean_csv_missing_columns() -> None:
    """Verify SyncError when critical columns are absent."""
    invalid_csv = "timestamp,exercise,kg\n2026-01-01,bench,50"
    with pytest.raises(SyncError, match="必須カラムが不足しています"):
        parse_and_clean_csv(invalid_csv)


def test_parse_and_clean_csv_empty() -> None:
    """Verify SyncError when input string is blank."""
    with pytest.raises(SyncError, match="取得したCSVデータが空です"):
        parse_and_clean_csv("   ")


def test_parse_and_clean_csv_invalid_timestamps() -> None:
    """Verify SyncError when no valid timestamp exists."""
    invalid_ts_csv = "timestamp,name,load,volume\ninvalid_date,bench,50,10"
    with pytest.raises(SyncError, match="有効な日時"):
        parse_and_clean_csv(invalid_ts_csv)


def test_persist_dataframe_to_sqlite(temp_data_dir: Path, mock_csv_content: str) -> None:
    """Verify atomic persistence into SQLite and composite index creation."""
    db_file = temp_data_dir / "test_fitness.db"
    df = parse_and_clean_csv(mock_csv_content)

    count = persist_dataframe_to_sqlite(df, db_path=db_file)
    assert count == 4
    assert db_file.exists()

    with closing(sqlite3.connect(db_file)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM workout_logs;")
        assert cursor.fetchone()[0] == 4

        # Verify composite index exists
        query = (
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_workout_logs_name_timestamp';"
        )
        cursor.execute(query)
        assert cursor.fetchone() is not None


def test_fetch_csv_content_detects_google_login() -> None:
    """Verify interception of Google account login redirect HTML."""
    html_response = b"<html><head><title>Google Accounts</title></head><body>Login</body></html>"
    mock_resp = MagicMock()
    mock_resp.read.return_value = html_response
    mock_resp.__enter__.return_value = mock_resp

    with (
        patch("urllib.request.urlopen", return_value=mock_resp),
        pytest.raises(SyncError, match="スプレッドシートが非公開またはログイン保護されています"),
    ):
        fetch_csv_content("https://dummy.url")


def test_sync_spreadsheet_to_sqlite_missing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify SyncError when spreadsheet ID is omitted."""
    monkeypatch.delenv("SPREADSHEET_ID", raising=False)
    with pytest.raises(SyncError, match="スプレッドシートIDが指定されていません"):
        sync_spreadsheet_to_sqlite(spreadsheet_id=None)


def test_persist_dataframe_to_sqlite_permission_error_existing(
    temp_data_dir: Path, mock_csv_content: str
) -> None:
    """Verify SyncError with actionable message when existing db file lacks write permissions."""
    db_file = temp_data_dir / "protected.db"
    db_file.write_bytes(b"")
    df = parse_and_clean_csv(mock_csv_content)

    real_open = open

    def fake_open(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        if str(file) == str(db_file) and "a" in mode:
            raise PermissionError(13, "Permission denied")
        return real_open(file, mode, *args, **kwargs)  # type: ignore[call-overload]

    with (
        patch("builtins.open", side_effect=fake_open),
        pytest.raises(SyncError, match="書き込み権限がありません"),
    ):
        persist_dataframe_to_sqlite(df, db_path=db_file)


def test_persist_dataframe_to_sqlite_permission_error_probe(
    temp_data_dir: Path, mock_csv_content: str
) -> None:
    """Verify SyncError when non-existent db target directory rejects probe file creation."""
    db_file = temp_data_dir / "subdir" / "new.db"
    df = parse_and_clean_csv(mock_csv_content)

    real_open = open

    def fake_open(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        if ".perm_check.tmp" in str(file):
            raise PermissionError(13, "Permission denied")
        return real_open(file, mode, *args, **kwargs)  # type: ignore[call-overload]

    with (
        patch("builtins.open", side_effect=fake_open),
        pytest.raises(SyncError, match="書き込み権限がありません"),
    ):
        persist_dataframe_to_sqlite(df, db_path=db_file)


def test_persist_dataframe_to_sqlite_readonly_sqlite_error(
    temp_data_dir: Path, mock_csv_content: str
) -> None:
    """Verify SyncError provides actionable diagnostic message when SQLite raises readonly error."""
    db_file = temp_data_dir / "readonly.db"
    df = parse_and_clean_csv(mock_csv_content)

    with (
        patch(
            "sqlite3.connect",
            side_effect=sqlite3.OperationalError("attempt to write a readonly database"),
        ),
        pytest.raises(SyncError, match="配置先フォルダ.*書き込み権限がありません"),
    ):
        persist_dataframe_to_sqlite(df, db_path=db_file)


def test_persist_dataframe_to_sqlite_clears_readonly_attribute(
    temp_data_dir: Path, mock_csv_content: str
) -> None:
    """Verify persist_dataframe_to_sqlite removes read-only attribute and succeeds."""
    db_file = temp_data_dir / "ro_file.db"
    db_file.write_bytes(b"")
    os.chmod(db_file, stat.S_IREAD)

    try:
        df = parse_and_clean_csv(mock_csv_content)
        count = persist_dataframe_to_sqlite(df, db_path=db_file)
        assert count == 4
    finally:
        os.chmod(db_file, stat.S_IWRITE)
