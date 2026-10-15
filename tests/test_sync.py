from __future__ import annotations

import sqlite3
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
