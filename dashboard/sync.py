from __future__ import annotations

import argparse
import os
import sqlite3
import stat
import sys
import urllib.error
import urllib.request
from contextlib import closing, suppress
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


class SyncError(Exception):
    """Domain exception raised during spreadsheet synchronization or SQLite persistence."""


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

    target_columns = ["timestamp", "name", "load", "volume", "mode", "calculated_volume"]
    return df[target_columns]


def persist_dataframe_to_sqlite(df: pd.DataFrame, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Atomic full-refresh persistence into SQLite with query index optimization.

    Args:
        df: Cleaned dataframe to write.
        db_path: Target SQLite database file path.

    Returns:
        int: Number of records persisted.

    Raises:
        SyncError: On SQLite transactional write failure.
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

            # 1. Load into staging table to protect active table
            df.to_sql("workout_logs_staging", conn, if_exists="replace", index=False)

            # 2. Atomic swap within transaction
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS workout_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    name TEXT NOT NULL,
                    load REAL,
                    volume REAL,
                    mode TEXT,
                    calculated_volume REAL
                );
            """)
            cursor.execute("DELETE FROM workout_logs;")
            cursor.execute("""
                INSERT INTO workout_logs (
                    timestamp, name, load, volume, mode, calculated_volume
                )
                SELECT timestamp, name, load, volume, mode, calculated_volume
                FROM workout_logs_staging;
            """)
            cursor.execute("DROP TABLE workout_logs_staging;")

            # 3. Create composite index for performant UI queries
            index_sql = (
                "CREATE INDEX IF NOT EXISTS idx_workout_logs_name_timestamp "
                "ON workout_logs (name, timestamp DESC);"
            )
            cursor.execute(index_sql)
            conn.commit()
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

    return len(df)


def sync_spreadsheet_to_sqlite(
    spreadsheet_id: str | None = None,
    sheet_name: str = DEFAULT_SHEET_NAME,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, object]:
    """Orchestrate end-to-end synchronization from Google Spreadsheet to SQLite.

    Args:
        spreadsheet_id: Google Spreadsheet ID (falls back to SPREADSHEET_ID env var).
        sheet_name: Name of the sheet tab.
        db_path: Target SQLite file path.

    Returns:
        dict[str, object]: Success flag, synced record count, and receipt message.

    Raises:
        SyncError: On missing configuration or pipeline failure.
    """
    target_id = spreadsheet_id or os.getenv("SPREADSHEET_ID")
    if not target_id or not target_id.strip():
        msg = (
            "スプレッドシートIDが指定されていません。"
            ".env ファイルまたはサイドバー入力で設定してください。"
        )
        raise SyncError(msg)

    url = build_csv_url(target_id, sheet_name=sheet_name)
    csv_text = fetch_csv_content(url)
    cleaned_df = parse_and_clean_csv(csv_text)
    record_count = persist_dataframe_to_sqlite(cleaned_df, db_path=db_path)

    return {
        "success": True,
        "count": record_count,
        "message": f"正常に {record_count} 件の運動ログを同期しました。",
    }


def main() -> None:
    """CLI entry point for standalone ETL executions."""
    desc = "Sync workout logs from Google Spreadsheet to SQLite."
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument("--spreadsheet-id", type=str, default=None, help="Google Spreadsheet ID")
    parser.add_argument("--sheet-name", type=str, default=DEFAULT_SHEET_NAME, help="Sheet tab name")
    parser.add_argument(
        "--db-path", type=str, default=str(DEFAULT_DB_PATH), help="Target SQLite path"
    )
    args = parser.parse_args()

    try:
        result = sync_spreadsheet_to_sqlite(
            spreadsheet_id=args.spreadsheet_id,
            sheet_name=args.sheet_name,
            db_path=Path(args.db_path),
        )
        print(f"[SUCCESS] {result['message']}")
    except SyncError as e:
        print(f"[ERROR] {e!s}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
