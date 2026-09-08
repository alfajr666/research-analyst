"""Make the application source directory importable during tests."""
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "research_analyst"
sys.path.insert(0, str(SOURCE_ROOT))


@pytest.fixture(autouse=True)
def disable_live_only_15m_override(monkeypatch):
    """Keep production-only local .env overrides out of unit-test defaults."""
    import config

    monkeypatch.setattr(config, "STRUCTURAL_15M_ZONES_ENABLED", False)
