"""
Canonical HTML test fixtures for AllAccess ticket checker and parser tests.
"""
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent


def load_fixture(filename: str) -> str:
    """Load a fixture file by name from the fixtures directory."""
    path = FIXTURES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Fixture file not found: {path}")
    return path.read_text(encoding="utf-8")
