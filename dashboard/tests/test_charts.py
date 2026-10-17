from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import pytest

from app import (
    aggregate_daily_logs,
    extract_selected_date,
    is_aerobic_exercise,
    render_aerobic_chart,
    render_strength_chart,
)


@pytest.fixture
def sample_workout_df() -> pd.DataFrame:
    """Provide minimal chronological dataframe for testing Altair chart synthesis."""
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-01-01 10:00", "2026-01-02 10:00"]),
            "name": ["チェストプレス", "チェストプレス"],
            "load": [50.0, 55.0],
            "volume": [10.0, 12.0],
            "calculated_volume": [500.0, 660.0],
            "mode": ["通常", "通常"],
        }
    )


@pytest.mark.parametrize(
    ("exercise_name", "expected"),
    [
        ("トレッドミル", True),
        ("エアロバイク 30分", True),
        ("Running Machine", True),
        ("Night Walk", True),
        ("チェストプレス", False),
        ("レッグプレス", False),
        ("ラットプルダウン", False),
        ("", False),
    ],
)
def test_is_aerobic_exercise(exercise_name: str, expected: bool) -> None:
    """Verify aerobic exercise keyword classification across variants and edge cases."""
    # Arrange & Act
    result = is_aerobic_exercise(exercise_name)

    # Assert
    assert result is expected


def test_aggregate_daily_logs_empty() -> None:
    """Verify aggregate_daily_logs handles empty DataFrame returning expected schema."""
    # Arrange
    df = pd.DataFrame()

    # Act
    result = aggregate_daily_logs(df, is_aerobic=False)

    # Assert
    assert result.empty
    assert list(result.columns) == [
        "date_str",
        "timestamp",
        "name",
        "load",
        "volume",
        "mode",
        "calculated_volume",
        "set_count",
    ]


def test_aggregate_daily_logs_strength() -> None:
    """Verify daily aggregation for strength: sum of reps, max load, total calculated volume."""
    # Arrange: 3 sets on 2026-10-16 and 1 set on 2026-10-17
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-10-16 10:00",
                    "2026-10-16 10:05",
                    "2026-10-16 10:10",
                    "2026-10-17 11:00",
                ]
            ),
            "name": ["チェストプレス"] * 4,
            "load": [50.0, 55.0, 60.0, 55.0],
            "volume": [10.0, 10.0, 10.0, 12.0],
            "calculated_volume": [500.0, 550.0, 600.0, 660.0],
            "mode": ["ウォームアップ", "通常", "限界", ""],
        }
    )

    # Act
    aggregated = aggregate_daily_logs(df, is_aerobic=False)

    # Assert
    assert len(aggregated) == 2

    day1 = aggregated.iloc[0]
    assert day1["date_str"] == "2026-10-16"
    assert day1["name"] == "チェストプレス"
    assert day1["volume"] == 30.0  # 10 + 10 + 10 reps
    assert day1["load"] == 60.0  # max load
    assert day1["calculated_volume"] == 1650.0  # 500 + 550 + 600 kg
    assert day1["set_count"] == 3
    assert day1["mode"] == "ウォームアップ, 通常, 限界"

    day2 = aggregated.iloc[1]
    assert day2["date_str"] == "2026-10-17"
    assert day2["volume"] == 12.0
    assert day2["load"] == 55.0
    assert day2["set_count"] == 1
    assert day2["mode"] == "通常"  # fallback when mode is empty


def test_aggregate_daily_logs_aerobic() -> None:
    """Verify daily aggregation for aerobic workouts: sum of duration and max heart rate."""
    # Arrange: 2 sessions on same day
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-10-16 09:00", "2026-10-16 18:00"]),
            "name": ["トレッドミル", "トレッドミル"],
            "load": [135.0, 145.0],  # Heart rate (bpm)
            "volume": [20.0, 15.0],  # Duration (min)
            "mode": ["ラン", "ウォーク"],
        }
    )

    # Act
    aggregated = aggregate_daily_logs(df, is_aerobic=True)

    # Assert
    assert len(aggregated) == 1
    row = aggregated.iloc[0]
    assert row["date_str"] == "2026-10-16"
    assert row["volume"] == 35.0  # 20 + 15 min
    assert row["load"] == 145.0  # max heart rate
    assert row["calculated_volume"] == 0.0  # 0 for aerobic
    assert row["set_count"] == 2


def test_aggregate_daily_logs_all_na_load() -> None:
    """Verify fallback to 0.0 load when all load values in group are NaN."""
    # Arrange
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-10-16 10:00"]),
            "name": ["自重スクワット"],
            "load": [float("nan")],
            "volume": [20.0],
            "mode": [None],
        }
    )

    # Act
    aggregated = aggregate_daily_logs(df, is_aerobic=False)

    # Assert
    assert len(aggregated) == 1
    assert aggregated.iloc[0]["load"] == 0.0
    assert aggregated.iloc[0]["mode"] == "通常"


class FakeEvent:
    """Mock container for Streamlit chart selection events."""

    def __init__(self, selection: Any) -> None:
        self.selection = selection


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (None, None),
        ("not_an_event", None),
        (FakeEvent(selection=None), None),
        (FakeEvent(selection="not_a_dict"), None),
        (FakeEvent(selection={}), None),
        (FakeEvent(selection={"date_selection": []}), None),
        (FakeEvent(selection={"date_selection": ["not_a_dict"]}), None),
        (FakeEvent(selection={"date_selection": [{}]}), None),
        (FakeEvent(selection={"date_selection": [{"date_str": "2026-10-16"}]}), "2026-10-16"),
        (
            FakeEvent(selection={"date_selection": [{"timestamp": "2026-10-16T10:00:00"}]}),
            "2026-10-16",
        ),
        (
            FakeEvent(selection={"date_selection": [{"date": 1792108800000}]}),
            pd.to_datetime(1792108800000, unit="ms").strftime("%Y-%m-%d"),
        ),
    ],
)
def test_extract_selected_date(event: object, expected: str | None) -> None:
    """Verify extract_selected_date parses date from diverse Streamlit selection payloads."""
    # Act
    result = extract_selected_date(event, param_name="date_selection")

    # Assert
    assert result == expected


def test_render_strength_chart_valid_schema(sample_workout_df: pd.DataFrame) -> None:
    """Verify render_strength_chart builds a valid LayerChart conforming to Vega-Lite schema."""
    # Arrange
    df = sample_workout_df

    # Act
    chart = render_strength_chart(df)
    schema_dict = chart.to_dict(validate=True)

    # Assert
    assert isinstance(chart, alt.LayerChart)
    assert "layer" in schema_dict
    assert len(schema_dict["layer"]) == 2

    # Check selection parameter presence in params
    params = schema_dict.get("params", [])
    param_names = [p.get("name") for p in params]
    assert "date_selection" in param_names

    # Check bar corner radius in compiled Vega-Lite spec
    bar_layer = schema_dict["layer"][0]
    assert bar_layer["mark"]["type"] == "bar"
    assert bar_layer["mark"]["cornerRadiusTopLeft"] == 4
    assert bar_layer["mark"]["cornerRadiusTopRight"] == 4


def test_render_aerobic_chart_valid_schema(sample_workout_df: pd.DataFrame) -> None:
    """Verify render_aerobic_chart builds a valid LayerChart conforming to Vega-Lite schema."""
    # Arrange
    df = sample_workout_df

    # Act
    chart = render_aerobic_chart(df)
    schema_dict = chart.to_dict(validate=True)

    # Assert
    assert isinstance(chart, alt.LayerChart)
    assert "layer" in schema_dict
    assert len(schema_dict["layer"]) == 2

    # Check selection parameter presence in params
    params = schema_dict.get("params", [])
    param_names = [p.get("name") for p in params]
    assert "date_selection" in param_names

    # Check bar corner radius in compiled Vega-Lite spec
    bar_layer = schema_dict["layer"][0]
    assert bar_layer["mark"]["type"] == "bar"
    assert bar_layer["mark"]["cornerRadiusTopLeft"] == 4
    assert bar_layer["mark"]["cornerRadiusTopRight"] == 4


def test_render_charts_fallback_without_timestamp() -> None:
    """Verify chart rendering gracefully falls back when dataframe lacks timestamp."""
    # Arrange
    df = pd.DataFrame(
        {
            "load": [50.0],
            "volume": [10.0],
            "calculated_volume": [500.0],
            "mode": ["通常"],
        }
    )

    # Act & Assert: Should not raise
    chart_strength = render_strength_chart(df)
    chart_aerobic = render_aerobic_chart(df)
    assert isinstance(chart_strength, alt.LayerChart)
    assert isinstance(chart_aerobic, alt.LayerChart)
