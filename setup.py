"""Build the console into wheels; installed users need only Python and a browser."""

import os
import shutil
import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithConsole(build_py):
    def run(self):
        root = Path(__file__).parent
        ui = root / "ui"
        if os.environ.get("ENTRYPLUG_SKIP_UI_BUILD") != "1":
            npm = shutil.which("npm")
            if not npm:
                raise RuntimeError(
                    "Building from source requires Node 22+ and npm; "
                    "install a wheel to avoid build tools"
                )
            if not (ui / "node_modules").is_dir():
                subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=ui, check=True)
            subprocess.run([npm, "run", "build"], cwd=ui, check=True)
        elif not (root / "src/entryplug_ui/static/index.html").is_file():
            raise RuntimeError("ENTRYPLUG_SKIP_UI_BUILD requires already compiled console assets")
        # Incremental wheels must not retain chunks removed by Vite's clean build.
        built_assets = Path(self.build_lib) / "entryplug_ui" / "static"
        if built_assets.exists():
            shutil.rmtree(built_assets)
        super().run()


setup(cmdclass={"build_py": BuildWithConsole})
