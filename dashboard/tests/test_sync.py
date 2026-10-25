from __future__ import annotations

import os
import sqlite3
import stat
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sync import (
    SyncError,
    SyncInterlockError,
    SyncStats,
    build_csv_url,
    fetch_csv_content,
    generate_record_id,
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


def test_generate_record_id() -> None:
    """Verify deterministic SHA-256 derivation for record identifiers."""
    id1 = generate_record_id("2026-03-29 10:00:00", "チェストプレス")
    id2 = generate_record_id(" 2026-03-29 10:00:00 ", "チェストプレス ")
    id3 = generate_record_id("2026-03-29 10:00:00", "レッグプレス")
    assert id1 == id2
    assert len(id1) == 16
    assert id1 != id3


def test_parse_and_clean_csv_success(mock_csv_content: str) -> None:
    """Verify standard CSV parsing, calculated volume calculation, and record_id derivation."""
    df = parse_and_clean_csv(mock_csv_content)
    assert len(df) == 4
    assert list(df.columns) == [
        "record_id",
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
    assert len(df.iloc[0]["record_id"]) == 16


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


def test_parse_and_clean_csv_deduplicates_identical_rows(mock_csv_content: str) -> None:
    """Verify parse_and_clean_csv removes duplicate rows with identical timestamp and name."""
    duplicated_csv = mock_csv_content + "2026-03-01 10:00:00,チェストプレス,40,10,両手\n"
    df = parse_and_clean_csv(duplicated_csv)
    assert len(df) == 4


def test_persist_dataframe_to_sqlite(temp_data_dir: Path, mock_csv_content: str) -> None:
    """Verify atomic persistence into SQLite and composite index creation."""
    db_file = temp_data_dir / "test_fitness.db"
    df = parse_and_clean_csv(mock_csv_content)

    stats = persist_dataframe_to_sqlite(df, db_path=db_file)
    assert stats.inserted == 4
    assert stats.updated == 0
    assert stats.unchanged == 0
    assert stats.total_db == 4
    assert int(stats) == 4
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


def test_persist_dataframe_to_sqlite_incremental_upsert(
    temp_data_dir: Path, mock_csv_content: str
) -> None:
    """Verify UPSERT behaviour: insert new records and update existing ones without full wipe."""
    db_file = temp_data_dir / "test_upsert.db"
    initial_df = parse_and_clean_csv(mock_csv_content)
    stats1 = persist_dataframe_to_sqlite(initial_df, db_path=db_file)
    assert stats1.inserted == 4
    assert stats1.total_db == 4

    # Second pass: Modify 1 record, keep 3 unchanged, and add 1 new record
    updated_csv = mock_csv_content + "\n2026-03-30 12:00:00,アブベンチ,0,25,"
    # Alter the load of first row (40.0 -> 50.0)
    updated_csv = updated_csv.replace(
        "2026-03-01 10:00:00,チェストプレス,40,10,両手",
        "2026-03-01 10:00:00,チェストプレス,50,10,両手",
    )
    second_df = parse_and_clean_csv(updated_csv)
    assert len(second_df) == 5

    stats2 = persist_dataframe_to_sqlite(second_df, db_path=db_file)
    assert stats2.inserted == 1
    assert stats2.updated == 1
    assert stats2.unchanged == 3
    assert stats2.total_db == 5

    # Verify updated value in database
    with closing(sqlite3.connect(db_file)) as conn:
        cursor = conn.cursor()
        query = (
            "SELECT load, calculated_volume FROM workout_logs "
            "WHERE name='チェストプレス' AND timestamp='2026-03-01 10:00:00';"
        )
        cursor.execute(query)
        row = cursor.fetchone()
        assert row[0] == 50.0
        assert row[1] == 500.0


def test_persist_dataframe_to_sqlite_interlock_and_force(temp_data_dir: Path) -> None:
    """Verify SyncInterlockError on sudden drop, and bypass when force_full_refresh is enabled."""
    db_file = temp_data_dir / "test_interlock.db"

    # Pre-populate with 25 records (2 days to stay within valid hours)
    csv_rows = ["timestamp,name,load,volume,mode"]
    for i in range(25):
        day = (i // 20) + 1
        hour = i % 20
        csv_rows.append(f"2026-01-{day:02d} {hour:02d}:00:00,種目_{i},10,10,通常")
    large_csv = "\n".join(csv_rows)

    large_df = parse_and_clean_csv(large_csv)
    stats = persist_dataframe_to_sqlite(large_df, db_path=db_file)
    assert stats.total_db == 25

    # Prepare truncated dataset with only 5 records (< 50% of 25)
    small_csv = "\n".join(csv_rows[:6])  # header + 5 rows
    small_df = parse_and_clean_csv(small_csv)
    assert len(small_df) == 5

    # 1. Normal sync must raise SyncInterlockError and keep 25 records
    with pytest.raises(SyncInterlockError, match="安全装置（激減インターロック）が作動しました"):
        persist_dataframe_to_sqlite(small_df, db_path=db_file)

    with closing(sqlite3.connect(db_file)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM workout_logs;")
        assert cursor.fetchone()[0] == 25

    # 2. Force full refresh bypasses interlock and replaces records
    stats_forced = persist_dataframe_to_sqlite(small_df, db_path=db_file, force_full_refresh=True)
    assert stats_forced.inserted == 5
    assert stats_forced.total_db == 5

    with closing(sqlite3.connect(db_file)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM workout_logs;")
        assert cursor.fetchone()[0] == 5


def test_persist_dataframe_to_sqlite_buffer_mode(temp_data_dir: Path) -> None:
    """Verify buffer mode allows incoming rows to decrease without triggering SyncInterlockError."""
    db_file = temp_data_dir / "test_buffer_mode.db"

    # Pre-populate with 25 records
    csv_rows = ["timestamp,name,load,volume,mode"]
    for i in range(25):
        day = (i // 20) + 1
        hour = i % 20
        csv_rows.append(f"2026-01-{day:02d} {hour:02d}:00:00,種目_{i},10,10,通常")
    large_csv = "\n".join(csv_rows)
    large_df = parse_and_clean_csv(large_csv)
    stats1 = persist_dataframe_to_sqlite(large_df, db_path=db_file)
    assert stats1.total_db == 25

    # Incoming dataset: 3 existing records (duplicates) + 2 new records = 5 records (< 50% of 25)
    buffer_rows = [
        "timestamp,name,load,volume,mode",
        "2026-01-01 00:00:00,種目_0,10,10,通常",  # Duplicate (unchanged)
        "2026-01-01 01:00:00,種目_1,10,10,通常",  # Duplicate (unchanged)
        "2026-01-01 02:00:00,種目_2,10,10,通常",  # Duplicate (unchanged)
        "2026-02-01 10:00:00,新種目_A,50,10,通常",  # New record
        "2026-02-01 11:00:00,新種目_B,60,10,通常",  # New record
    ]
    buffer_df = parse_and_clean_csv("\n".join(buffer_rows))
    assert len(buffer_df) == 5

    # Ingest in buffer mode: bypass interlock, keep 25 records, insert 2, unchanged 3
    stats2 = persist_dataframe_to_sqlite(buffer_df, db_path=db_file, allow_buffer_mode=True)
    assert stats2.inserted == 2
    assert stats2.updated == 0
    assert stats2.unchanged == 3
    assert stats2.total_db == 27

    with closing(sqlite3.connect(db_file)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM workout_logs;")
        assert cursor.fetchone()[0] == 27


def test_schema_migration_from_legacy_table(temp_data_dir: Path, mock_csv_content: str) -> None:
    """Verify seamless migration of legacy workout_logs schema without record_id."""
    db_file = temp_data_dir / "legacy_fitness.db"

    # Create legacy table without record_id
    with closing(sqlite3.connect(db_file)) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE workout_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                name TEXT NOT NULL,
                load REAL,
                volume REAL,
                mode TEXT,
                calculated_volume REAL
            );
        """)
        cursor.execute("""
            INSERT INTO workout_logs (timestamp, name, load, volume, mode, calculated_volume)
            VALUES ('2026-03-01 10:00:00', 'チェストプレス', 40.0, 10.0, '通常', 400.0);
        """)
        conn.commit()

    df = parse_and_clean_csv(mock_csv_content)
    stats = persist_dataframe_to_sqlite(df, db_path=db_file)
    # 1 existing record updated/unchanged, other 3 inserted
    assert stats.total_db == 4

    # Verify migrated table structure
    with closing(sqlite3.connect(db_file)) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(workout_logs);")
        columns = [row[1] for row in cursor.fetchall()]
        assert "record_id" in columns
        assert "updated_at" in columns


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


def test_sync_spreadsheet_to_sqlite_end_to_end(temp_data_dir: Path, mock_csv_content: str) -> None:
    """Verify full orchestration of sync_spreadsheet_to_sqlite with pre-sync backup."""
    db_file = temp_data_dir / "e2e.db"
    mock_resp = MagicMock()
    mock_resp.read.return_value = mock_csv_content.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with (
        patch("urllib.request.urlopen", return_value=mock_resp),
        patch("backup_manager.run_backup", return_value={"success": True}) as mock_backup,
    ):
        # 1st run: no db exists yet, auto_backup not triggered on non-existent db
        res1 = sync_spreadsheet_to_sqlite(
            spreadsheet_id="test_sheet",
            db_path=db_file,
            auto_backup=True,
        )
        assert res1["success"] is True
        assert res1["count"] == 4
        stats1 = res1["stats"]
        assert isinstance(stats1, SyncStats)
        assert stats1.inserted == 4

        # 2nd run: db exists, auto_backup is triggered
        res2 = sync_spreadsheet_to_sqlite(
            spreadsheet_id="test_sheet",
            db_path=db_file,
            auto_backup=True,
        )
        assert res2["success"] is True
        assert mock_backup.called
        assert "事前バックアップ作成済" in str(res2["message"])


def test_sync_spreadsheet_to_sqlite_buffer_mode(temp_data_dir: Path, mock_csv_content: str) -> None:
    """Verify sync_spreadsheet_to_sqlite with allow_buffer_mode enabled."""
    db_file = temp_data_dir / "buffer_e2e.db"
    mock_resp = MagicMock()
    mock_resp.read.return_value = mock_csv_content.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = sync_spreadsheet_to_sqlite(
            spreadsheet_id="test_sheet",
            db_path=db_file,
            allow_buffer_mode=True,
            auto_backup=False,
        )
        assert res["success"] is True
        assert "累積追記同期" in str(res["message"])


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
        stats = persist_dataframe_to_sqlite(df, db_path=db_file)
        assert stats.total_db == 4
    finally:
        os.chmod(db_file, stat.S_IWRITE)


def test_persist_dataframe_to_sqlite_mkdir_oserror(
    temp_data_dir: Path, mock_csv_content: str
) -> None:
    """Verify SyncError when directory creation fails with OSError."""
    db_file = temp_data_dir / "unreachable" / "fail.db"
    df = parse_and_clean_csv(mock_csv_content)

    with (
        patch.object(Path, "mkdir", side_effect=OSError("Disk failure")),
        pytest.raises(SyncError, match="データベース配置ディレクトリの作成に失敗しました"),
    ):
        persist_dataframe_to_sqlite(df, db_path=db_file)


def test_sync_settings_defaults() -> None:
    """Verify SyncSettings initializes deterministic default paths."""
    from sync import SyncSettings

    settings = SyncSettings()
    assert settings.sheet_name == "Log"
    assert settings.db_path.name == "choco_fitness.db"


def test_workout_record_schema_validation() -> None:
    """Verify WorkoutRecordSchema validates strictly and fails on invalid input."""
    import pydantic

    from sync import WorkoutRecordSchema

    # Valid entry
    valid = WorkoutRecordSchema(
        record_id="0123456789abcdef",
        timestamp="2026-03-01 10:00:00",
        name="ベンチプレス",
        load=80.0,
        volume=10.0,
        mode="通常",
        calculated_volume=800.0,
    )
    assert valid.name == "ベンチプレス"

    # Invalid timestamp format
    with pytest.raises(pydantic.ValidationError):
        WorkoutRecordSchema(
            record_id="0123456789abcdef",
            timestamp="invalid-time",
            name="ベンチプレス",
        )


def test_fetch_csv_content_retry_on_urlerror() -> None:
    """Verify fetch_csv_content retries on transient URLError and succeeds."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"timestamp,name,load,volume\n"
    mock_resp.__enter__.return_value = mock_resp

    call_count = 0

    def mock_urlopen(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise urllib.error.URLError("Connection reset by peer")
        return mock_resp

    with (
        patch("urllib.request.urlopen", side_effect=mock_urlopen),
        patch("tenacity.nap.time.sleep", return_value=None),
    ):
        result = fetch_csv_content("https://dummy.url")
        assert "timestamp,name,load,volume" in result
        assert call_count == 2
