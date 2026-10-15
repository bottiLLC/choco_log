from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture
def temp_data_dir() -> Generator[Path]:
    """Provide an isolated temporary directory for data and backups."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir)
        data_dir = path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        yield data_dir


@pytest.fixture
def mock_csv_content() -> str:
    """Return a standard multi-exercise CSV sample."""
    return """timestamp,name,load,volume,mode
2026-03-01 10:00:00,チェストプレス,40,10,両手
2026-03-05 10:00:00,チェストプレス,45,10,両手
2026-03-08 10:00:00,トレッドミル,130,20,
2026-03-12 10:00:00,レッグプレス,80,12,両足
"""
