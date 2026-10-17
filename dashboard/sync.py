from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import stat
import sys
import urllib.error
import urllib.request
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pandas as pd
from dotenv import load_dotenv

# Deterministic sys.path setup
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env file relative to project root
load_dotenv(_ROOT / ".env")

DEFAULT_DB_PATH: Final[Path] = _ROOT / "data" / "choco_fitness.db"
DEFAULT_SHEET_NAME: Final[str] = "Log"
REQUEST_TIMEOUT_SECONDS: Final[int] = 15
DROP_THRESHOLD_RATIO: Final[float] = 0.5
MIN_RECORDS_FOR_INTERLOCK: Final[int] = 20


class SyncError(Exception):
    """Domain exception raised during spreadsheet synchronization or SQLite persistence."""


class SyncInterlockError(SyncError):
    """Exception raised when safety invariants (e.g. abrupt record count drop) are violated."""


@dataclass(frozen=True, slots=True)
class SyncStats:
    """Metrics container tracking incremental upsert outcomes."""

    inserted: int
    updated: int
    unchanged: int
    total_db: int

    def __int__(self) -> int:
        """Compatibility cast returning total active database records."""
        return self.total_db


def generate_record_id(timestamp: str, name: str) -> str:
    """Generate deterministic 16-hex character primary key from timestamp and name.

    Args:
        timestamp: ISO 8601 formatted datetime string.
        name: Workout exercise identifier.

    Returns:
        str: 16-character deterministic hex digest.
    """
    raw_key = f"{timestamp.strip()}|{name.strip()}".encode()
    return hashlib.sha256(raw_key).hexdigest()[:16]


def build_csv_url(spreadsheet_id: str, sheet_name: str = DEFAULT_SHEET_NAME) -> str:
    """Construct Google Spreadsheet export CSV URL.

    Args:
        spreadsheet_id: Target Google Spreadsheet identifier.
        sheet_name: Target sheet name within the spreadsheet.

    Returns:
        str: Fully qualified export URL.
    """
    clean_id = spreadsheet_id.strip()
    base_url = "https://docs.google.com/spreadsheets/d"
    return f"{base_url}/{clean_id}/gviz/tq?tqx=out:csv&sheet={sheet_name}"


def fetch_csv_content(url: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> str:
    """Download raw CSV text from Google Visualization API endpoint.

    Args:
        url: Direct CSV export endpoint URL.
        timeout: Maximum network wait time in seconds.

    Returns:
        str: Raw CSV content decoded as UTF-8.

    Raises:
        SyncError: On network failure, 401/403 authorization error, or unexpected HTML response.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ChocoLog/1.0",
        "Accept": "text/csv,text/plain,*/*",
    }
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_bytes: bytes = response.read()
            text: str = content_bytes.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 302):
            msg = (
                f"スプレッドシートへのアクセス権限がありません（HTTP {e.code}）。"
                "共有設定を「リンクを知っている全員（閲覧者）」に変更してください。"
            )
            raise SyncError(msg) from e
        raise SyncError(f"スプレッドシートの取得に失敗しました（HTTP {e.code}: {e.reason}）") from e
    except urllib.error.URLError as e:
        raise SyncError(f"スプレッドシート接続エラー: {e.reason}") from e
    except TimeoutError as e:
        msg = "スプレッドシート取得がタイムアウトしました。通信環境を確認してください。"
        raise SyncError(msg) from e

    # Interlock: Detect Google login redirect page delivered as text/html
    if "<html" in text.lower() or "accounts.google.com" in text or "servicelogin" in text.lower():
        raise SyncError(
            "スプレッドシートが非公開またはログイン保護されています。"
            "共有設定を「リンクを知っている全員（閲覧可）」に設定してください。"
        )

    return text


def parse_and_clean_csv(csv_text: str) -> pd.DataFrame:
    """Parse CSV text and enforce data contract for workout logging.

    Args:
        csv_text: Raw CSV string.

    Returns:
        pd.DataFrame: Cleaned dataframe with validated columns and datatypes.

    Raises:
        SyncError: When required columns are missing or parsing fails completely.
    """
    import io

    if not csv_text.strip():
        msg = "取得したCSVデータが空です。スプレッドシートにデータが存在するか確認してください。"
        raise SyncError(msg)

    try:
        df = pd.read_csv(io.StringIO(csv_text), dtype=str)
    except Exception as e:
        raise SyncError(f"CSVパースエラー: {e!s}") from e

    # Normalize column headers
    df.columns = df.columns.str.strip().str.lower()

    required_columns = {"timestamp", "name", "load", "volume"}
    missing = required_columns - set(df.columns)
    if missing:
        msg = (
            f"必須カラムが不足しています: {missing} "
            "(スプシ側ヘッダーを確認してください: timestamp, name, load, volume, mode)"
        )
        raise SyncError(msg)

    # Optional mode column
    if "mode" not in df.columns:
        df["mode"] = ""
    else:
        df["mode"] = df["mode"].fillna("").astype(str).str.strip()

    # Parse timestamps and eliminate rows with invalid datetime
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).copy()

    if df.empty:
        raise SyncError("有効な日時（timestamp）を含むレコードが1件も存在しません。")

    # Clean name column
    df["name"] = df["name"].fillna("").astype(str).str.strip()
    df = df[df["name"] != ""].copy()

    # Numeric conversions with NaN fallback
    df["load"] = pd.to_numeric(df["load"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    # Calculate derived training volume (e.g. load * volume)
    df["calculated_volume"] = df["load"] * df["volume"]

    # Format timestamp to ISO 8601 string for SQLite compatibility
    df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

    # Chronological sort
    df = df.sort_values(by="timestamp", ascending=True).reset_index(drop=True)

    # Assign deterministic primary key
    df["record_id"] = [
        generate_record_id(str(ts), str(nm))
        for ts, nm in zip(df["timestamp"], df["name"], strict=False)
    ]

    target_columns = [
        "record_id",
        "timestamp",
        "name",
        "load",
        "volume",
        "mode",
        "calculated_volume",
    ]
    return df[target_columns]


def ensure_schema_and_migrate(conn: sqlite3.Connection) -> None:
    """Ensure workout_logs table exists and migrate legacy schema to support record_id primary key.

    Args:
        conn: Active SQLite connection.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='workout_logs';")
    table_exists = cursor.fetchone() is not None

    if not table_exists:
        cursor.execute("""
            CREATE TABLE workout_logs (
                record_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                name TEXT NOT NULL,
                load REAL,
                volume REAL,
                mode TEXT,
                calculated_volume REAL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
    else:
        cursor.execute("PRAGMA table_info(workout_logs);")
        existing_columns = {str(row[1]) for row in cursor.fetchall()}
        if "record_id" not in existing_columns:
            # Execute schema migration from legacy table
            cursor.execute(
                "SELECT timestamp, name, load, volume, mode, calculated_volume FROM workout_logs;"
            )
            legacy_rows = cursor.fetchall()
            cursor.execute("""
                CREATE TABLE workout_logs_v2 (
                    record_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    name TEXT NOT NULL,
                    load REAL,
                    volume REAL,
                    mode TEXT,
                    calculated_volume REAL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)
            migrated_rows = [
                (
                    generate_record_id(str(r[0]), str(r[1])),
                    r[0],
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                )
                for r in legacy_rows
            ]
            cursor.executemany(
                """
                INSERT OR IGNORE INTO workout_logs_v2 (
                    record_id, timestamp, name, load, volume, mode, calculated_volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                migrated_rows,
            )
            cursor.execute("DROP TABLE workout_logs;")
            cursor.execute("ALTER TABLE workout_logs_v2 RENAME TO workout_logs;")

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_workout_logs_name_timestamp
        ON workout_logs (name, timestamp DESC);
    """)


def persist_dataframe_to_sqlite(
    df: pd.DataFrame,
    db_path: Path = DEFAULT_DB_PATH,
    force_full_refresh: bool = False,
    drop_threshold_ratio: float = DROP_THRESHOLD_RATIO,
    min_records_for_interlock: int = MIN_RECORDS_FOR_INTERLOCK,
) -> SyncStats:
    """Persist dataframe into SQLite via incremental UPSERT with drop interlock.

    Args:
        df: Cleaned dataframe to write.
        db_path: Target SQLite database file path.
        force_full_refresh: If True, bypasses interlock and replaces table entirely.
        drop_threshold_ratio: Ratio threshold below which interlock triggers.
        min_records_for_interlock: Minimum existing records required to enforce interlock.

    Returns:
        SyncStats: Inserted, updated, unchanged, and total database record metrics.

    Raises:
        SyncInterlockError: When incoming count drops below safety threshold.
        SyncError: On filesystem permission or SQLite transactional write failure.
    """
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SyncError(f"データベース配置ディレクトリの作成に失敗しました: {e!s}") from e

    # Clear OS-level read-only attribute if set on existing database file
    if db_path.exists():
        with suppress(OSError):
            os.chmod(db_path, stat.S_IWRITE | stat.S_IREAD)

    # Pre-flight check: verify write permission on database file or parent directory
    try:
        if db_path.exists():
            with open(db_path, "a"):
                pass
        else:
            test_probe = db_path.parent / ".perm_check.tmp"
            with open(test_probe, "w"):
                pass
            test_probe.unlink(missing_ok=True)
    except PermissionError as e:
        err_detail = e.strerror or str(e)
        raise SyncError(
            f"SQLiteデータベース更新エラー: 書き込み権限がありません ({err_detail})。"
            f"対象パス: '{db_path}'。"
            "保護されたフォルダ（管理者権限が必要なフォルダ等）に配置されている場合は、"
            "書き込み権限のある一般ユーザーフォルダに移動するか管理者として実行してください。"
        ) from e

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON;")

            # 1. Ensure table schema and execute backward-compatible migration
            ensure_schema_and_migrate(conn)

            # 2. Count existing records
            cursor.execute("SELECT COUNT(*) FROM workout_logs;")
            existing_count = int(cursor.fetchone()[0])
            incoming_count = len(df)

            # 3. Interlock: Protect against accidental deletion in Google Spreadsheet
            if (
                not force_full_refresh
                and existing_count >= min_records_for_interlock
                and incoming_count < int(existing_count * drop_threshold_ratio)
            ):
                pct = int(drop_threshold_ratio * 100)
                msg = (
                    f"安全装置（激減インターロック）が作動しました: "
                    f"スプレッドシート側の取得件数 ({incoming_count}件) が "
                    f"既存DB件数 ({existing_count}件) の {pct}% 未満に急減しています。"
                    "誤削除の可能性があるため同期を中断しました。意図的な削除の場合は強制同期を実行してください。"
                )
                raise SyncInterlockError(msg)

            # 4. Mode branch: Full wipe vs Incremental UPSERT
            if force_full_refresh:
                cursor.execute("DELETE FROM workout_logs;")
                insert_sql = """
                    INSERT INTO workout_logs (
                        record_id, timestamp, name, load, volume, mode, calculated_volume
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """
                records = [
                    (
                        row.record_id,
                        row.timestamp,
                        row.name,
                        row.load,
                        row.volume,
                        row.mode,
                        row.calculated_volume,
                    )
                    for row in df.itertuples(index=False)
                ]
                cursor.executemany(insert_sql, records)
                conn.commit()
                return SyncStats(
                    inserted=incoming_count,
                    updated=0,
                    unchanged=0,
                    total_db=incoming_count,
                )

            # 5. Staging table for atomic diff calculation and upsert
            df.to_sql("workout_logs_staging", conn, if_exists="replace", index=False)

            # Calculate inserted count
            cursor.execute("""
                SELECT COUNT(*) FROM workout_logs_staging s
                WHERE NOT EXISTS (
                    SELECT 1 FROM workout_logs w WHERE w.record_id = s.record_id
                );
            """)
            inserted_count = int(cursor.fetchone()[0])

            # Calculate updated count (matching key but altered values)
            cursor.execute("""
                SELECT COUNT(*) FROM workout_logs_staging s
                JOIN workout_logs w ON w.record_id = s.record_id
                WHERE w.load IS NOT s.load
                   OR w.volume IS NOT s.volume
                   OR w.mode IS NOT s.mode
                   OR w.calculated_volume IS NOT s.calculated_volume;
            """)
            updated_count = int(cursor.fetchone()[0])

            # Execute UPSERT into workout_logs
            upsert_sql = """
                INSERT INTO workout_logs (
                    record_id, timestamp, name, load, volume, mode, calculated_volume, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(record_id) DO UPDATE SET
                    load = excluded.load,
                    volume = excluded.volume,
                    mode = excluded.mode,
                    calculated_volume = excluded.calculated_volume,
                    updated_at = CURRENT_TIMESTAMP
                WHERE load IS NOT excluded.load
                   OR volume IS NOT excluded.volume
                   OR mode IS NOT excluded.mode
                   OR calculated_volume IS NOT excluded.calculated_volume;
            """
            records = [
                (
                    row.record_id,
                    row.timestamp,
                    row.name,
                    row.load,
                    row.volume,
                    row.mode,
                    row.calculated_volume,
                )
                for row in df.itertuples(index=False)
            ]
            cursor.executemany(upsert_sql, records)

            cursor.execute("DROP TABLE workout_logs_staging;")
            cursor.execute("SELECT COUNT(*) FROM workout_logs;")
            total_db_count = int(cursor.fetchone()[0])
            conn.commit()

            unchanged_count = max(0, incoming_count - (inserted_count + updated_count))
            return SyncStats(
                inserted=inserted_count,
                updated=updated_count,
                unchanged=unchanged_count,
                total_db=total_db_count,
            )

    except (SyncError, SyncInterlockError):
        raise
    except sqlite3.Error as e:
        err_msg = str(e)
        if "readonly" in err_msg.lower():
            target_desc = f"'{db_path.name}' または配置先フォルダ '{db_path.parent}'"
            raise SyncError(
                f"SQLiteデータベース更新エラー: {err_msg}。"
                f"データベースファイル {target_desc} に書き込み権限がありません。"
                "保護されたフォルダ（管理者権限が必要なフォルダ等）に配置されている場合は、"
                "書き込み権限のある一般ユーザーフォルダに移動するか管理者として実行してください。"
            ) from e
        raise SyncError(f"SQLiteデータベース更新エラー: {e!s}") from e


def sync_spreadsheet_to_sqlite(
    spreadsheet_id: str | None = None,
    sheet_name: str = DEFAULT_SHEET_NAME,
    db_path: Path = DEFAULT_DB_PATH,
    force_full_refresh: bool = False,
    auto_backup: bool = True,
) -> dict[str, object]:
    """Orchestrate end-to-end synchronization from Google Spreadsheet to SQLite.

    Args:
        spreadsheet_id: Google Spreadsheet ID (falls back to SPREADSHEET_ID env var).
        sheet_name: Name of the sheet tab.
        db_path: Target SQLite file path.
        force_full_refresh: If True, executes full wipe and replaces all records.
        auto_backup: If True, triggers pre-sync ZIP backup of ./data before writing.

    Returns:
        dict[str, object]: Success flag, synced record count, stats, and receipt message.

    Raises:
        SyncError: On missing configuration or pipeline failure.
        SyncInterlockError: On volume drop detection.
    """
    target_id = spreadsheet_id or os.getenv("SPREADSHEET_ID")
    if not target_id or not target_id.strip():
        msg = (
            "スプレッドシートIDが指定されていません。"
            ".env ファイルまたはサイドバー入力で設定してください。"
        )
        raise SyncError(msg)

    # Pre-sync automatic snapshot backup if database exists
    backup_info: str = ""
    if auto_backup and db_path.exists():
        from backup_manager import run_backup

        backup_res = run_backup(app_name="choco_presync", source_dir=db_path.parent)
        if backup_res.get("success"):
            backup_info = "（事前バックアップ作成済）"

    url = build_csv_url(target_id, sheet_name=sheet_name)
    csv_text = fetch_csv_content(url)
    cleaned_df = parse_and_clean_csv(csv_text)
    stats = persist_dataframe_to_sqlite(
        cleaned_df,
        db_path=db_path,
        force_full_refresh=force_full_refresh,
    )

    mode_label = "全量洗い替え" if force_full_refresh else "差分同期"
    msg = (
        f"正常に{mode_label}を完了しました: 新規 {stats.inserted} 件, "
        f"更新 {stats.updated} 件, 維持 {stats.unchanged} 件 "
        f"(DB合計: {stats.total_db} 件){backup_info}。"
    )

    return {
        "success": True,
        "count": stats.total_db,
        "stats": stats,
        "message": msg,
    }


def main() -> None:
    """CLI entry point for standalone ETL executions."""
    desc = "Sync workout logs from Google Spreadsheet to SQLite via incremental UPSERT."
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument("--spreadsheet-id", type=str, default=None, help="Google Spreadsheet ID")
    parser.add_argument("--sheet-name", type=str, default=DEFAULT_SHEET_NAME, help="Sheet tab name")
    parser.add_argument(
        "--db-path", type=str, default=str(DEFAULT_DB_PATH), help="Target SQLite path"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force full table refresh bypassing volume drop interlock",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Disable automatic pre-sync ZIP backup",
    )
    args = parser.parse_args()

    try:
        result = sync_spreadsheet_to_sqlite(
            spreadsheet_id=args.spreadsheet_id,
            sheet_name=args.sheet_name,
            db_path=Path(args.db_path),
            force_full_refresh=args.force,
            auto_backup=not args.no_backup,
        )
        print(f"[SUCCESS] {result['message']}")
    except SyncError as e:
        print(f"[ERROR] {e!s}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
