from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Deterministic sys.path setup
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backup_manager import get_backup_dir, run_backup, set_backup_dir
from sync import (
    DEFAULT_DB_PATH,
    SyncError,
    build_csv_url,
    fetch_csv_content,
    parse_and_clean_csv,
    persist_dataframe_to_sqlite,
)

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
def load_exercise_logs(
    db_path_str: str, exercise_name: str, mode: str = "全件", period: str = "全期間"
) -> pd.DataFrame:
    """Fetch chronological workout logs for an exercise with mode and period filters.

    Args:
        db_path_str: Path string to SQLite database.
        exercise_name: Target exercise name.
        mode: Target mode ('全件' for all modes).
        period: Time period filter ('全期間', '直近90日', '直近30日').

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

            if period == "直近30日":
                cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
                query += " AND timestamp >= ?"
                params.append(cutoff)
            elif period == "直近90日":
                cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
                query += " AND timestamp >= ?"
                params.append(cutoff)

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


def aggregate_daily_logs(df: pd.DataFrame, is_aerobic: bool = False) -> pd.DataFrame:
    """Aggregate per-set workout logs into deterministic daily summaries.

    Args:
        df: Raw workout records containing timestamp, load, volume, and mode.
        is_aerobic: True for cardiovascular exercises, False for resistance training.

    Returns:
        pd.DataFrame: Daily aggregated records with summed volume and peak load metrics.
    """
    if df.empty:
        return pd.DataFrame(
            columns=[
                "date_str",
                "timestamp",
                "name",
                "load",
                "volume",
                "mode",
                "calculated_volume",
                "set_count",
            ]
        )

    work_df = df.copy()
    work_df["date_str"] = pd.to_datetime(work_df["timestamp"]).dt.strftime("%Y-%m-%d")

    records: list[dict[str, Any]] = []
    for date_str, group in work_df.groupby("date_str", sort=True):
        total_vol = float(group["volume"].sum())
        max_load = float(group["load"].max()) if group["load"].notna().any() else 0.0
        calc_vol = (
            float(group["calculated_volume"].sum())
            if "calculated_volume" in group.columns and not is_aerobic
            else 0.0
        )
        unique_modes = [str(m).strip() for m in group["mode"].dropna().unique() if str(m).strip()]
        mode_repr = ", ".join(unique_modes) if unique_modes else "通常"
        exercise_name = str(group["name"].iloc[0])

        records.append(
            {
                "date_str": str(date_str),
                "timestamp": group["timestamp"].max(),
                "name": exercise_name,
                "load": max_load,
                "volume": total_vol,
                "mode": mode_repr,
                "calculated_volume": calc_vol,
                "set_count": len(group),
            }
        )

    return pd.DataFrame(records)


def extract_selected_date(event: object, param_name: str = "date_selection") -> str | None:
    """Extract selected date string from Streamlit Altair event payload.

    Args:
        event: Selection event returned by st.altair_chart with on_select='rerun'.
        param_name: Altair selection parameter identifier.

    Returns:
        str | None: Selected date in 'YYYY-MM-DD' format, or None if unselected.
    """
    if not event or not hasattr(event, "selection"):
        return None

    selection_dict = getattr(event, "selection", None)
    if selection_dict is None or not hasattr(selection_dict, "get"):
        return None

    selected_points = selection_dict.get(param_name, [])
    if not isinstance(selected_points, list) or not selected_points:
        return None

    first_point = selected_points[0]
    if not isinstance(first_point, dict):
        return None

    raw_val = first_point.get("date_str") or first_point.get("timestamp") or first_point.get("date")
    if raw_val is None:
        return None

    if isinstance(raw_val, str):
        return raw_val[:10]
    if isinstance(raw_val, (int, float)):
        return pd.to_datetime(raw_val, unit="ms").strftime("%Y-%m-%d")

    return None


def render_strength_chart(
    df: pd.DataFrame, selection_name: str = "date_selection"
) -> alt.LayerChart:
    """Build modern dual-axis Altair chart for daily aggregated resistance training.

    Args:
        df: Daily aggregated dataframe for the selected exercise.
        selection_name: Identifier for Altair selection point parameter.

    Returns:
        alt.LayerChart: Layered chart resolving independent Y axes with selection support.
    """
    chart_df = df.copy()
    if "date_str" not in chart_df.columns:
        if "timestamp" in chart_df.columns:
            chart_df["date_str"] = pd.to_datetime(chart_df["timestamp"]).dt.strftime("%Y-%m-%d")
        else:
            chart_df["date_str"] = chart_df.index.astype(str)

    select_date = alt.selection_point(name=selection_name, fields=["date_str"])

    base = alt.Chart(chart_df).encode(
        x=alt.X(
            "date_str:N",
            title="実施日",
            axis=alt.Axis(labelAngle=-30, grid=False),
        )
    )

    # Volume bar chart (Right Y-axis, sleek modern indigo)
    bar = base.mark_bar(
        size=18,
        cornerRadiusTopLeft=4,
        cornerRadiusTopRight=4,
    ).encode(
        y=alt.Y(
            "volume:Q",
            title="合計回数 (reps)",
            axis=alt.Axis(titleColor="#6366F1", grid=False),
        ),
        color=alt.value("#6366F1"),
        opacity=alt.condition(select_date, alt.value(0.85), alt.value(0.35)),
        tooltip=[
            alt.Tooltip("date_str:N", title="実施日"),
            alt.Tooltip("volume:Q", title="合計回数 (reps)", format=".0f"),
            alt.Tooltip("load:Q", title="最高重量 (kg)", format=".1f"),
            alt.Tooltip("calculated_volume:Q", title="総ボリューム (kg)", format=".1f"),
            alt.Tooltip("set_count:Q", title="セット数", format=".0f"),
            alt.Tooltip("mode:N", title="モード"),
        ],
    )

    # Load line chart with points (Left Y-axis, vibrant rose)
    line = base.mark_line(
        point=alt.OverlayMarkDef(filled=True, size=70, color="#F43F5E"),
        color="#F43F5E",
        strokeWidth=3,
        interpolate="monotone",
    ).encode(
        y=alt.Y(
            "load:Q",
            title="最高重量 (kg)",
            scale=alt.Scale(zero=False),
            axis=alt.Axis(titleColor="#F43F5E"),
        ),
        tooltip=[
            alt.Tooltip("date_str:N", title="実施日"),
            alt.Tooltip("volume:Q", title="合計回数 (reps)", format=".0f"),
            alt.Tooltip("load:Q", title="最高重量 (kg)", format=".1f"),
            alt.Tooltip("calculated_volume:Q", title="総ボリューム (kg)", format=".1f"),
            alt.Tooltip("set_count:Q", title="セット数", format=".0f"),
            alt.Tooltip("mode:N", title="モード"),
        ],
    )

    chart = (
        alt.layer(bar, line)
        .add_params(select_date)
        .resolve_scale(y="independent")
        .properties(
            width="container",
            height=440,
            title=alt.TitleParams(
                text=("日次 重量・合計回数推移（筋力トレーニング） - バーをクリックして内訳を表示"),
                fontSize=14,
                color="#94A3B8",
            ),
        )
    )
    return cast(alt.LayerChart, chart)


def render_aerobic_chart(
    df: pd.DataFrame, selection_name: str = "date_selection"
) -> alt.LayerChart:
    """Build modern dual-axis Altair chart for daily aggregated aerobic exercises.

    Args:
        df: Daily aggregated dataframe for the selected exercise.
        selection_name: Identifier for Altair selection point parameter.

    Returns:
        alt.LayerChart: Layered chart resolving independent Y axes with selection support.
    """
    chart_df = df.copy()
    if "date_str" not in chart_df.columns:
        if "timestamp" in chart_df.columns:
            chart_df["date_str"] = pd.to_datetime(chart_df["timestamp"]).dt.strftime("%Y-%m-%d")
        else:
            chart_df["date_str"] = chart_df.index.astype(str)

    select_date = alt.selection_point(name=selection_name, fields=["date_str"])

    base = alt.Chart(chart_df).encode(
        x=alt.X(
            "date_str:N",
            title="実施日",
            axis=alt.Axis(labelAngle=-30, grid=False),
        )
    )

    # Duration bar chart (Right Y-axis, teal)
    bar = base.mark_bar(
        size=18,
        cornerRadiusTopLeft=4,
        cornerRadiusTopRight=4,
    ).encode(
        y=alt.Y(
            "volume:Q",
            title="合計運動時間 (min)",
            axis=alt.Axis(titleColor="#14B8A6", grid=False),
        ),
        color=alt.value("#14B8A6"),
        opacity=alt.condition(select_date, alt.value(0.85), alt.value(0.35)),
        tooltip=[
            alt.Tooltip("date_str:N", title="実施日"),
            alt.Tooltip("load:Q", title="最高心拍数 (bpm)", format=".0f"),
            alt.Tooltip("volume:Q", title="合計運動時間 (min)", format=".1f"),
            alt.Tooltip("set_count:Q", title="セッション数", format=".0f"),
            alt.Tooltip("mode:N", title="モード"),
        ],
    )

    # Heart rate line chart (Left Y-axis, amber)
    line = base.mark_line(
        point=alt.OverlayMarkDef(filled=True, size=70, color="#F59E0B"),
        color="#F59E0B",
        strokeWidth=3,
        interpolate="monotone",
    ).encode(
        y=alt.Y(
            "load:Q",
            title="最高心拍数 (bpm)",
            scale=alt.Scale(zero=False),
            axis=alt.Axis(titleColor="#F59E0B"),
        ),
        tooltip=[
            alt.Tooltip("date_str:N", title="実施日"),
            alt.Tooltip("load:Q", title="最高心拍数 (bpm)", format=".0f"),
            alt.Tooltip("volume:Q", title="合計運動時間 (min)", format=".1f"),
            alt.Tooltip("set_count:Q", title="セッション数", format=".0f"),
            alt.Tooltip("mode:N", title="モード"),
        ],
    )

    chart = (
        alt.layer(bar, line)
        .add_params(select_date)
        .resolve_scale(y="independent")
        .properties(
            width="container",
            height=440,
            title=alt.TitleParams(
                text=(
                    "日次 心拍数・合計運動時間推移（有酸素トレーニング） "
                    "- バーをクリックして内訳を表示"
                ),
                fontSize=14,
                color="#94A3B8",
            ),
        )
    )
    return cast(alt.LayerChart, chart)


@st.dialog("スプレッドシートデータ全量同期の確認")
def confirm_sync_dialog(sheet_id: str) -> None:
    """Show modal confirmation dialog for full-refresh spreadsheet sync.

    Args:
        sheet_id: Target Google Spreadsheet ID to sync from.
    """
    st.write("スプレッドシートから最新データを取得し、ローカルデータベースを全量洗い替えます。")
    st.caption("※ 障害時・通信エラー時は既存データが自動保護されます。")

    if st.button("全量同期を実行する", type="primary", use_container_width=True):
        with st.status("スプレッドシートと同期中...", expanded=True) as status:
            try:
                st.write("📡 スプレッドシートからCSVデータを取得中...")
                url = build_csv_url(sheet_id)
                csv_text = fetch_csv_content(url)

                st.write("🧹 データ型変換・バリデーション検証中...")
                cleaned_df = parse_and_clean_csv(csv_text)

                st.write("💾 SQLiteデータベースへアトミック全量更新中...")
                count = persist_dataframe_to_sqlite(cleaned_df, db_path=DEFAULT_DB_PATH)

                status.update(
                    label="同期完了！最新データを反映しました。",
                    state="complete",
                    expanded=False,
                )
                st.cache_data.clear()
                st.toast(f"✅ 正常に {count} 件のログを同期しました！", icon="🏋️")
                st.rerun()
            except SyncError as e:
                status.update(label="同期失敗", state="error", expanded=True)
                st.error(f"同期エラー: {e!s}")


@st.fragment
def render_sync_section() -> None:
    """Render isolated reactive sync form widget inside sidebar."""
    st.header("⚙️ データ同期設定")
    with st.form("sync_form", clear_on_submit=False):
        sheet_id_input = st.text_input(
            "スプレッドシートID",
            value=DEFAULT_SPREADSHEET_ID,
            help="GoogleスプレッドシートのURLに含まれるID（共有設定：リンクを知っている全員が閲覧可）。",
        )
        submit_sync = st.form_submit_button("今すぐスプシから最新データを同期", type="primary")

    if submit_sync:
        clean_id = sheet_id_input.strip()
        if not clean_id:
            st.error("スプレッドシートIDを入力してください。")
        else:
            confirm_sync_dialog(clean_id)


@st.fragment
def render_backup_section() -> None:
    """Render isolated reactive data protection and backup widget inside sidebar."""
    st.divider()
    st.subheader("データ保護・バックアップ")

    current_dir = str(get_backup_dir())
    new_dir = st.text_input("保存先フォルダ", value=current_dir)
    if new_dir != current_dir and st.button("保存先パスを更新", use_container_width=True):
        save_res = set_backup_dir(new_dir)
        if save_res["success"]:
            st.success(str(save_res["message"]))
            st.toast("バックアップ保存先を更新しました", icon="📁")
            st.rerun()
        else:
            st.error(str(save_res["message"]))

    if st.button("今すぐバックアップを実行", use_container_width=True):
        with st.spinner("圧縮・整合性検証中..."):
            backup_res = run_backup(app_name="choco_fitness", source_dir=str(_ROOT / "data"))
        if backup_res["success"]:
            st.success(str(backup_res["message"]))
            st.caption(f"完了日時: {backup_res['timestamp']}")
            st.caption(f"保存先: {backup_res['destination']}")
            st.toast("バックアップが完了しました！", icon="💾")
        else:
            st.error(str(backup_res["message"]))


def main() -> None:
    """Render the main Streamlit application layout and reactive state."""
    st.set_page_config(
        page_title="ChocoLog Fitness",
        page_icon="🏋️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Top Brand Header
    st.title("🏋️ ChocoLog 運動ログ分析ダッシュボード")
    st.caption("Googleスプレッドシート連携型 筋力・有酸素トレーニング推移分析ツール")

    db_path_str = str(DEFAULT_DB_PATH)

    # Sidebar: Isolated Fragments
    with st.sidebar:
        render_sync_section()

        st.divider()
        st.header("🔍 表示フィルタ")

        available_exercises = load_available_exercises(db_path_str)
        selected_exercise: str | None = None
        selected_mode: str = "全件"
        selected_period: str = "全期間"

        if available_exercises:
            selected_exercise = st.selectbox(
                "種目を選択",
                options=available_exercises,
                index=0,
                help="分析・可視化対象のトレーニング種目を選択してください。",
            )

            if selected_exercise:
                available_modes = load_available_modes(db_path_str, selected_exercise)
                if len(available_modes) > 1:
                    st.caption("トレーニングモード")
                    mode_val = st.pills(
                        "モード",
                        options=available_modes,
                        default=available_modes[0],
                        label_visibility="collapsed",
                    )
                    selected_mode = mode_val or available_modes[0]
                else:
                    selected_mode = "全件"

                st.caption("表示期間")
                period_val = st.segmented_control(
                    "期間",
                    options=["全期間", "直近90日", "直近30日"],
                    default="全期間",
                    label_visibility="collapsed",
                )
                selected_period = period_val or "全期間"
        else:
            st.info("データがありません。上の同期ボタンを押してデータを読み込んでください。")

        render_backup_section()

    if not selected_exercise:
        st.info(
            "サイドバーの「今すぐスプシから最新データを同期」を実行し、分析対象の種目を選択してください。"
        )
        return

    df = load_exercise_logs(
        db_path_str,
        selected_exercise,
        mode=selected_mode,
        period=selected_period,
    )

    if df.empty:
        warn_msg = (
            f"「{selected_exercise}」({selected_mode} / {selected_period}) "
            "のデータは見つかりませんでした。"
        )
        st.warning(warn_msg)
        return

    is_aerobic = is_aerobic_exercise(selected_exercise)
    load_unit = "bpm" if is_aerobic else "kg"
    volume_unit = "min" if is_aerobic else "reps"

    daily_df = aggregate_daily_logs(df, is_aerobic=is_aerobic)

    # 1. Header Metrics Card Section
    st.subheader(f"📊 {selected_exercise} のトレーニング実績")
    latest_daily = daily_df.iloc[-1]
    prev_daily = daily_df.iloc[-2] if len(daily_df) >= 2 else None

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric(
            label="直近セッション実施日",
            value=str(latest_daily["date_str"]),
        )

    with col2:
        latest_load = latest_daily["load"]
        prev_load = prev_daily["load"] if prev_daily is not None else None
        load_delta = (
            f"{latest_load - prev_load:+.1f} {load_unit}"
            if (prev_load is not None and pd.notna(latest_load) and pd.notna(prev_load))
            else None
        )
        st.metric(
            label=f"直近最高負荷 ({load_unit})",
            value=f"{latest_load:.1f}" if pd.notna(latest_load) else "-",
            delta=load_delta,
        )

    with col3:
        latest_vol = latest_daily["volume"]
        prev_vol = prev_daily["volume"] if prev_daily is not None else None
        vol_delta = (
            f"{latest_vol - prev_vol:+.1f} {volume_unit}"
            if (prev_vol is not None and pd.notna(latest_vol) and pd.notna(prev_vol))
            else None
        )
        st.metric(
            label=f"直近合計量 ({volume_unit})",
            value=f"{latest_vol:.0f}" if pd.notna(latest_vol) else "-",
            delta=vol_delta,
        )

    with col4:
        if not is_aerobic:
            latest_calc = latest_daily["calculated_volume"]
            prev_calc = prev_daily["calculated_volume"] if prev_daily is not None else None
            calc_delta = (
                f"{latest_calc - prev_calc:+.1f} kg"
                if (prev_calc is not None and pd.notna(latest_calc) and pd.notna(prev_calc))
                else None
            )
            st.metric(
                label="直近総ボリューム (kg)",
                value=f"{latest_calc:.1f}" if pd.notna(latest_calc) else "-",
                delta=calc_delta,
            )
        else:
            st.metric(label="種目分類", value="有酸素運動")

    st.divider()

    # 2. Modern Tabbed Workspace Layout
    tab_chart, tab_table, tab_stats = st.tabs(
        ["📈 経過推移チャート", "📋 過去ログ履歴一覧", "🏆 個人ベスト & 統計"]
    )

    with tab_chart:
        chart = render_aerobic_chart(daily_df) if is_aerobic else render_strength_chart(daily_df)
        chart_event = st.altair_chart(
            chart,
            use_container_width=True,
            on_select="rerun",
            key=f"workout_chart_{selected_exercise}_{selected_mode}_{selected_period}",
        )

        selected_date = extract_selected_date(chart_event)

        st.divider()

        # Drilldown table section
        breakdown_df: pd.DataFrame
        if selected_date:
            breakdown_mask = df["timestamp"].dt.strftime("%Y-%m-%d") == selected_date
            breakdown_df = df[breakdown_mask].copy()
            st.subheader(f"📋 {selected_date} のトレーニング内訳 ({len(breakdown_df)} 件)")
        else:
            latest_date = str(daily_df["date_str"].iloc[-1]) if not daily_df.empty else None
            if latest_date:
                breakdown_mask = df["timestamp"].dt.strftime("%Y-%m-%d") == latest_date
                breakdown_df = df[breakdown_mask].copy()
                st.subheader(f"📋 直近セッション ({latest_date}) の内訳 ({len(breakdown_df)} 件)")
                st.caption(
                    "💡 グラフ上の日付バーまたはポイントをクリックすると、"
                    "該当日のセット内訳が表示されます。"
                )
            else:
                breakdown_df = pd.DataFrame()

        if not breakdown_df.empty:
            breakdown_display = breakdown_df.sort_values(by="timestamp", ascending=True).copy()
            breakdown_cols = {
                "timestamp": st.column_config.DatetimeColumn(
                    "記録日時", format="YYYY-MM-DD HH:mm", width="medium"
                ),
                "name": st.column_config.TextColumn("種目名", width="medium"),
                "load": st.column_config.NumberColumn(
                    f"負荷 ({load_unit})", format=f"%.1f {load_unit}"
                ),
                "volume": st.column_config.NumberColumn(
                    f"量 ({volume_unit})", format=f"%.0f {volume_unit}"
                ),
                "mode": st.column_config.TextColumn("モード", width="small"),
                "calculated_volume": st.column_config.NumberColumn(
                    "総ボリューム (kg)", format="%.1f kg"
                ),
            }
            if is_aerobic:
                breakdown_display = breakdown_display.drop(
                    columns=["calculated_volume"], errors="ignore"
                )
                breakdown_cols.pop("calculated_volume", None)

            st.dataframe(
                breakdown_display,
                column_config=breakdown_cols,
                use_container_width=True,
                hide_index=True,
            )

    with tab_table:
        display_df = df.sort_values(by="timestamp", ascending=False).copy()

        column_configs = {
            "timestamp": st.column_config.DatetimeColumn(
                "記録日時", format="YYYY-MM-DD HH:mm", width="medium"
            ),
            "name": st.column_config.TextColumn("種目名", width="medium"),
            "load": st.column_config.NumberColumn(
                f"負荷 ({load_unit})", format=f"%.1f {load_unit}"
            ),
            "volume": st.column_config.NumberColumn(
                f"量 ({volume_unit})", format=f"%.0f {volume_unit}"
            ),
            "mode": st.column_config.TextColumn("モード", width="small"),
            "calculated_volume": st.column_config.NumberColumn(
                "総ボリューム (kg)", format="%.1f kg"
            ),
        }

        if is_aerobic:
            display_df = display_df.drop(columns=["calculated_volume"], errors="ignore")
            column_configs.pop("calculated_volume", None)

        st.dataframe(
            display_df,
            column_config=column_configs,
            use_container_width=True,
            hide_index=True,
        )

    with tab_stats:
        s_col1, s_col2, s_col3, s_col4 = st.columns(4)
        with s_col1:
            st.metric("総セッション数", f"{len(df)} 回")

        max_load = df["load"].max()
        with s_col2:
            st.metric(
                f"最高記録 ({load_unit})",
                f"{max_load:.1f} {load_unit}" if pd.notna(max_load) else "-",
            )

        with s_col3:
            if not is_aerobic:
                max_vol = df["calculated_volume"].max()
                st.metric(
                    "最高ボリューム",
                    f"{max_vol:.1f} kg" if pd.notna(max_vol) else "-",
                )
            else:
                max_duration = df["volume"].max()
                st.metric(
                    "最長運動時間",
                    f"{max_duration:.0f} min" if pd.notna(max_duration) else "-",
                )

        with s_col4:
            first_load = df.iloc[0]["load"]
            if pd.notna(first_load) and pd.notna(latest_load) and first_load > 0:
                growth_rate = ((latest_load - first_load) / first_load) * 100
                st.metric("初回比 成長率", f"{growth_rate:+.1f} %")
            else:
                st.metric("初回比 成長率", "-")


if __name__ == "__main__":
    main()
