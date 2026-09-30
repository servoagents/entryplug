"""Reject missing or stale console assets in a built wheel, including rebuilds."""

import argparse
import zipfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("wheel", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1] / "src/entryplug_ui/static"
expected = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
with zipfile.ZipFile(args.wheel) as wheel:
    actual = {
        name.removeprefix("entryplug_ui/static/")
        for name in wheel.namelist()
        if name.startswith("entryplug_ui/static/") and not name.endswith("/")
    }
if not expected or actual != expected:
    raise SystemExit(
        f"Wheel asset mismatch: missing={sorted(expected - actual)}, "
        f"stale={sorted(actual - expected)}"
    )
print(f"Wheel contains exactly {len(actual)} current console files, without stale chunks.")
