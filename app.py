from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Final, cast

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Deterministic sys.path setup
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backup_manager import get_backup_dir, run_backup, set_backup_dir
from sync import DEFAULT_DB_PATH, SyncError, sync_spreadsheet_to_sqlite

# Load environment variables
load_dotenv(_ROOT / ".env")

DEFAULT_SPREADSHEET_ID: Final[str] = os.getenv(
    "SPREADSHEET_ID", "1Q1FID9mYqSYUs8UQejVj82JS-qqHC79nQalSDbM7w2c"
)

AEROBIC_KEYWORDS: Final[tuple[str, ...]] = (
    "トレッドミル",
    "バイク",
    "エアロバイク",
    "ランニング",
    "ウォーキング",
    "有酸素",
    "クロストレーナー",
    "treadmill",
    "bike",
    "run",
    "walk",
)


@st.cache_data(show_spinner=False)
def load_available_exercises(db_path_str: str) -> list[str]:
    """Retrieve distinct exercise names present in the database.

    Args:
        db_path_str: Path string to the SQLite database.

    Returns:
        list[str]: Sorted list of unique exercise names.
    """
    db_path = Path(db_path_str)
    if not db_path.exists():
        return []

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            cursor = conn.cursor()
            # Check if table exists
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='workout_logs';"
            )
            if not cursor.fetchone():
                return []
            cursor.execute("SELECT DISTINCT name FROM workout_logs ORDER BY name ASC;")
            rows = cursor.fetchall()
            return [str(r[0]) for r in rows if r[0]]
    except sqlite3.Error:
        return []


@st.cache_data(show_spinner=False)
def load_available_modes(db_path_str: str, exercise_name: str) -> list[str]:
    """Retrieve distinct training modes for a given exercise.

    Args:
        db_path_str: Path string to the SQLite database.
        exercise_name: Target exercise name.

    Returns:
        list[str]: List containing '全件' and specific modes.
    """
    db_path = Path(db_path_str)
    if not db_path.exists():
        return ["全件"]

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            cursor = conn.cursor()
            query = (
                "SELECT DISTINCT mode FROM workout_logs "
                "WHERE name = ? AND mode != '' ORDER BY mode ASC;"
            )
            cursor.execute(query, (exercise_name,))
            rows = cursor.fetchall()
            modes = [str(r[0]) for r in rows if r[0]]
            return ["全件", *modes]
    except sqlite3.Error:
        return ["全件"]


@st.cache_data(show_spinner=False)
def load_exercise_logs(db_path_str: str, exercise_name: str, mode: str = "全件") -> pd.DataFrame:
    """Fetch chronological workout logs for an exercise with optional mode filter.

    Args:
        db_path_str: Path string to SQLite database.
        exercise_name: Target exercise name.
        mode: Target mode ('全件' for all modes).

    Returns:
        pd.DataFrame: Loaded records ordered chronologically.
    """
    db_path = Path(db_path_str)
    if not db_path.exists():
        return pd.DataFrame()

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            query = (
                "SELECT timestamp, name, load, volume, mode, calculated_volume "
                "FROM workout_logs WHERE name = ?"
            )
            params: list[str] = [exercise_name]

            if mode != "全件":
                query += " AND mode = ?"
                params.append(mode)

            query += " ORDER BY timestamp ASC;"
            df = pd.read_sql_query(query, conn, params=params)
            if not df.empty:
                df["timestamp"] = pd.to_datetime(df["timestamp"])
            return df
    except sqlite3.Error:
        return pd.DataFrame()


def is_aerobic_exercise(name: str) -> bool:
    """Determine whether an exercise is cardiovascular/aerobic based on name keywords.

    Args:
        name: Name of the exercise.

    Returns:
        bool: True if aerobic, False if strength/resistance training.
    """
    lower = name.lower()
    return any(keyword in lower for keyword in AEROBIC_KEYWORDS)


def render_strength_chart(df: pd.DataFrame) -> alt.LayerChart:
    """Build dual-axis Altair chart for resistance training (Load vs Volume).

    Args:
        df: Chronological dataframe for the selected exercise.

    Returns:
        alt.LayerChart: Layered chart resolving independent Y axes.
    """
    base = alt.Chart(df).encode(
        x=alt.X(
            "timestamp:T",
            title="記録日時",
            axis=alt.Axis(format="%Y-%m-%d %H:%M", labelAngle=-30),
        )
    )

    # Volume bar chart (Right Y-axis)
    bar = base.mark_bar(opacity=0.35, color="#4A90E2", size=18).encode(
        y=alt.Y(
            "calculated_volume:Q",
            title="総ボリューム (負荷 × 回数)",
            axis=alt.Axis(titleColor="#4A90E2"),
        ),
        tooltip=[
            alt.Tooltip("timestamp:T", title="日時", format="%Y-%m-%d %H:%M"),
            alt.Tooltip("load:Q", title="負荷 (kg)"),
            alt.Tooltip("volume:Q", title="回数 (reps)"),
            alt.Tooltip("calculated_volume:Q", title="ボリューム (kg)"),
            alt.Tooltip("mode:N", title="モード"),
        ],
    )

    # Load line chart with points (Left Y-axis)
    line = base.mark_line(point=True, color="#E94E77", strokeWidth=2.5).encode(
        y=alt.Y(
            "load:Q",
            title="負荷重量 (kg)",
            scale=alt.Scale(zero=False),
            axis=alt.Axis(titleColor="#E94E77"),
        ),
        tooltip=[
            alt.Tooltip("timestamp:T", title="日時", format="%Y-%m-%d %H:%M"),
            alt.Tooltip("load:Q", title="負荷 (kg)"),
            alt.Tooltip("volume:Q", title="回数 (reps)"),
            alt.Tooltip("calculated_volume:Q", title="ボリューム (kg)"),
            alt.Tooltip("mode:N", title="モード"),
        ],
    )

    chart = (
        alt.layer(bar, line)
        .resolve_scale(y="independent")
        .properties(
            width="container",
            height=420,
            title="重量・ボリューム推移（筋力トレーニング）",
        )
    )
    return cast(alt.LayerChart, chart)


def render_aerobic_chart(df: pd.DataFrame) -> alt.LayerChart:
    """Build dual-axis Altair chart for aerobic exercises (Heart Rate vs Duration).

    Args:
        df: Chronological dataframe for the selected exercise.

    Returns:
        alt.LayerChart: Layered chart resolving independent Y axes.
    """
    base = alt.Chart(df).encode(
        x=alt.X(
            "timestamp:T",
            title="記録日時",
            axis=alt.Axis(format="%Y-%m-%d %H:%M", labelAngle=-30),
        )
    )

    # Duration bar chart (Right Y-axis)
    bar = base.mark_bar(opacity=0.4, color="#50E3C2", size=18).encode(
        y=alt.Y(
            "volume:Q",
            title="運動時間 (min)",
            axis=alt.Axis(titleColor="#50E3C2"),
        ),
        tooltip=[
            alt.Tooltip("timestamp:T", title="日時", format="%Y-%m-%d %H:%M"),
            alt.Tooltip("load:Q", title="目標/心拍数 (bpm)"),
            alt.Tooltip("volume:Q", title="運動時間 (min)"),
            alt.Tooltip("mode:N", title="モード"),
        ],
    )

    # Heart rate line chart (Left Y-axis)
    line = base.mark_line(point=True, color="#F5A623", strokeWidth=2.5).encode(
        y=alt.Y(
            "load:Q",
            title="心拍数 (bpm)",
            scale=alt.Scale(zero=False),
            axis=alt.Axis(titleColor="#F5A623"),
        ),
        tooltip=[
            alt.Tooltip("timestamp:T", title="日時", format="%Y-%m-%d %H:%M"),
            alt.Tooltip("load:Q", title="心拍数 (bpm)"),
            alt.Tooltip("volume:Q", title="運動時間 (min)"),
            alt.Tooltip("mode:N", title="モード"),
        ],
    )

    chart = (
        alt.layer(bar, line)
        .resolve_scale(y="independent")
        .properties(
            width="container",
            height=420,
            title="心拍数・運動時間推移（有酸素トレーニング）",
        )
    )
    return cast(alt.LayerChart, chart)


def main() -> None:
    """Render the main Streamlit application layout and reactive state."""
    st.set_page_config(page_title="ChocoLog Fitness", page_icon="🏋️", layout="wide")

    # Header section
    st.title("🏋️ ChocoLog 運動ログ可視化ダッシュボード")
    st.caption("Googleスプレッドシート連携型 筋力・有酸素トレーニング推移分析ツール")

    db_path_str = str(DEFAULT_DB_PATH)

    # Sidebar: Synchronization, filtering, and data protection
    with st.sidebar:
        st.header("⚙️ データ同期設定")
        with st.form("sync_form", clear_on_submit=False):
            sheet_id_input = st.text_input(
                "スプレッドシートID",
                value=DEFAULT_SPREADSHEET_ID,
                help="GoogleスプレッドシートのURLに含まれるIDを入力してください（共有設定：リンクを知っている全員が閲覧可）。",
            )
            submit_sync = st.form_submit_button("今すぐスプシから最新データを同期", type="primary")

        if submit_sync:
            if not sheet_id_input.strip():
                st.error("スプレッドシートIDが入力されていません。")
            else:
                with st.spinner("スプレッドシートからデータを取得・更新中..."):
                    try:
                        res = sync_spreadsheet_to_sqlite(
                            spreadsheet_id=sheet_id_input.strip(),
                            db_path=DEFAULT_DB_PATH,
                        )
                        st.success(str(res["message"]))
                        st.cache_data.clear()
                        st.rerun()
                    except SyncError as e:
                        st.error(f"同期失敗: {e!s}")

        st.divider()
        st.header("🔍 表示フィルタ")

        available_exercises = load_available_exercises(db_path_str)
        selected_exercise: str | None = None
        selected_mode: str = "全件"

        if available_exercises:
            selected_exercise = st.selectbox(
                "種目を選択",
                options=available_exercises,
                index=0,
                help="可視化対象のトレーニング種目を選択してください。",
            )

            if selected_exercise:
                available_modes = load_available_modes(db_path_str, selected_exercise)
                if len(available_modes) > 1:
                    selected_mode = st.selectbox("モードを選択", options=available_modes, index=0)
                else:
                    selected_mode = "全件"
        else:
            st.info("データがありません。上の同期ボタンを押してデータを読み込んでください。")

        # Standardized Data Backup Section (Python-backup-script compliant)
        st.divider()
        st.subheader("データ保護・バックアップ")

        current_dir = str(get_backup_dir())
        new_dir = st.text_input("保存先フォルダ", value=current_dir)
        if new_dir != current_dir and st.button("保存先パスを更新", use_container_width=True):
            save_res = set_backup_dir(new_dir)
            if save_res["success"]:
                st.success(str(save_res["message"]))
                st.rerun()
            else:
                st.error(str(save_res["message"]))

        if st.button("今すぐバックアップを実行", use_container_width=True):
            with st.spinner("圧縮・整合性検証中..."):
                backup_res = run_backup(app_name="choco_fitness", source_dir="./data")
            if backup_res["success"]:
                st.success(str(backup_res["message"]))
                st.caption(f"完了日時: {backup_res['timestamp']}")
                st.caption(f"保存先: {backup_res['destination']}")
            else:
                st.error(str(backup_res["message"]))

    # Main Workspace: Metrics, Visualization, and History Table
    if not selected_exercise:
        st.info(
            "サイドバーの「今すぐスプシから最新データを同期」を実行し、分析対象の種目を選択してください。"
        )
        return

    df = load_exercise_logs(db_path_str, selected_exercise, mode=selected_mode)
    if df.empty:
        st.warning(f"「{selected_exercise}」({selected_mode}) のデータは見つかりませんでした。")
        return

    is_aerobic = is_aerobic_exercise(selected_exercise)
    load_unit = "bpm" if is_aerobic else "kg"
    volume_unit = "min" if is_aerobic else "reps"

    # 1. Header Metrics Card Section
    st.subheader(f"📊 {selected_exercise} のトレーニング実績")
    latest_record = df.iloc[-1]
    prev_record = df.iloc[-2] if len(df) >= 2 else None

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric(
            label="直近セッション日時",
            value=pd.to_datetime(latest_record["timestamp"]).strftime("%Y-%m-%d %H:%M"),
        )

    with col2:
        latest_load = latest_record["load"]
        load_delta = (
            f"{latest_load - prev_record['load']:+.1f} {load_unit}"
            if (prev_record is not None and pd.notna(latest_load) and pd.notna(prev_record["load"]))
            else None
        )
        st.metric(
            label=f"直近負荷 ({load_unit})",
            value=f"{latest_load:.1f}" if pd.notna(latest_load) else "-",
            delta=load_delta,
        )

    with col3:
        latest_vol = latest_record["volume"]
        prev_vol = prev_record["volume"] if prev_record is not None else None
        vol_delta = (
            f"{latest_vol - prev_vol:+.1f} {volume_unit}"
            if (prev_vol is not None and pd.notna(latest_vol) and pd.notna(prev_vol))
            else None
        )
        st.metric(
            label=f"直近量 ({volume_unit})",
            value=f"{latest_vol:.0f}" if pd.notna(latest_vol) else "-",
            delta=vol_delta,
        )

    with col4:
        if not is_aerobic:
            latest_calc = latest_record["calculated_volume"]
            calc_delta = (
                f"{latest_calc - prev_record['calculated_volume']:+.1f} kg"
                if (
                    prev_record is not None
                    and pd.notna(latest_calc)
                    and pd.notna(prev_record["calculated_volume"])
                )
                else None
            )
            st.metric(
                label="直近ボリューム (kg)",
                value=f"{latest_calc:.1f}" if pd.notna(latest_calc) else "-",
                delta=calc_delta,
            )
        else:
            st.metric(label="種目分類", value="有酸素運動")

    st.divider()

    # 2. Charts Section
    st.subheader("📈 経過推移チャート")
    chart = render_aerobic_chart(df) if is_aerobic else render_strength_chart(df)
    st.altair_chart(chart, use_container_width=True)

    st.divider()

    # 3. History Data Table Section
    st.subheader("📋 過去ログ履歴一覧")
    display_df = df.sort_values(by="timestamp", ascending=False).copy()
    display_df["timestamp"] = display_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M")

    rename_map = {
        "timestamp": "記録日時",
        "name": "種目名",
        "load": f"負荷 ({load_unit})",
        "volume": f"量 ({volume_unit})",
        "mode": "モード",
        "calculated_volume": "総ボリューム (kg)",
    }
    display_df = display_df.rename(columns=rename_map)

    if is_aerobic:
        display_df = display_df.drop(columns=["総ボリューム (kg)"], errors="ignore")

    st.dataframe(display_df, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
