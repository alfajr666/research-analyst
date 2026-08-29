"""Make the application source directory importable during tests."""
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "research_analyst"
sys.path.insert(0, str(SOURCE_ROOT))
