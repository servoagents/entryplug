import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import entryplug


def test_scenario_cli_waits_for_cleanup_after_interrupt(tmp_path):
    fixture = tmp_path / "containers/scenarios"
    fixture.mkdir(parents=True)
    (fixture / "run_suite.py").write_text("""import signal, time
from pathlib import Path

def stop(*args):
    time.sleep(0.6)
    Path("cleaned").write_text("done")
    raise SystemExit(130)

signal.signal(signal.SIGINT, stop)
Path("ready").write_text("ready")
while True:
    time.sleep(1)
""")
    driver = """from entryplug import cli
from pathlib import Path
import sys
cli.source_root = lambda: Path(sys.argv[1])
raise SystemExit(cli._test(cli._parse_args(["test", "--suite", "scenarios"])))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(entryplug.__file__).resolve().parents[1])
    process = subprocess.Popen(
        [sys.executable, "-c", driver, str(tmp_path)],
        env=environment,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        until = time.monotonic() + 10
        while not (tmp_path / "ready").exists():
            assert process.poll() is None
            assert time.monotonic() < until
            time.sleep(0.01)
        os.killpg(process.pid, signal.SIGINT)
        process.communicate(timeout=10)
        assert (tmp_path / "cleaned").exists(), (
            "CLI killed its runner before owned cleanup finished"
        )
        assert process.returncode == 130
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
