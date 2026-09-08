from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "refactoring" / "main" / "core"
sys.path.insert(0, str(CORE))

