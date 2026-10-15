from __future__ import annotations

import altair as alt
import pandas as pd
import pytest

from app import is_aerobic_exercise, render_aerobic_chart, render_strength_chart


@pytest.fixture
def sample_workout_df() -> pd.DataFrame:
    """Provide minimal chronological dataframe for testing Altair chart synthesis."""
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-01-01 10:00", "2026-01-02 10:00"]),
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
    # Check bar corner radius in compiled Vega-Lite spec
    bar_layer = schema_dict["layer"][0]
    assert bar_layer["mark"]["type"] == "bar"
    assert bar_layer["mark"]["cornerRadiusTopLeft"] == 4
    assert bar_layer["mark"]["cornerRadiusTopRight"] == 4
