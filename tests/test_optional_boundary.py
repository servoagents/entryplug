from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_core_import_does_not_import_optional_adapter_sdks() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import entryplug.episode; "
            "assert not {'mcp', 'openenv'} & sys.modules.keys()",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
