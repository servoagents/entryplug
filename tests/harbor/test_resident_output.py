"""A native diagnostic cannot become a private JSON control record."""

from __future__ import annotations

import json
import subprocess
import sys


def test_python_and_native_diagnostics_are_separate_from_control_records() -> None:
    script = """
import os
from entryplug_harbor.resident_output import private_record_stream
with private_record_stream() as records:
    print("python diagnostic", flush=True)
    os.write(1, b"native diagnostic\\n")
    records.write('{"version":1,"type":"ready"}\\n')
    records.flush()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, check=True)
    assert json.loads(result.stdout) == {"version": 1, "type": "ready"}
    assert result.stderr.splitlines() == [b"python diagnostic", b"native diagnostic"]


def test_record_stream_restores_stdout_when_owned_scope_ends() -> None:
    script = """
from entryplug_harbor.resident_output import private_record_stream
try:
    with private_record_stream() as records:
        print("diagnostic", flush=True)
        raise RuntimeError("exit scope")
except RuntimeError:
    print("restored", flush=True)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, check=True)
    assert result.stdout == b"restored\n"
    assert result.stderr == b"diagnostic\n"
