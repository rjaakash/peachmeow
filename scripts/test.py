import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import run_test_build

if __name__ == "__main__":
    run_test_build()
