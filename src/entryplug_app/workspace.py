"""Private XDG paths and a process-owned workspace lock."""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from entryplug_app.contracts import AppError


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".entryplug-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class Workspace:
    state: Path
    config: Path
    data: Path

    @classmethod
    def resolve(cls, name: str = "default") -> Workspace:
        if "/" in name or name.startswith("."):
            root = Path(name).expanduser().resolve()
            return cls(root / "state", root / "config", root / "data")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
            raise AppError("validation_error", "Invalid workspace name")
        home = Path.home()
        return cls(
            *(
                Path(os.environ.get(env, home / fallback)) / "entryplug" / name
                for env, fallback in (
                    ("XDG_STATE_HOME", ".local/state"),
                    ("XDG_CONFIG_HOME", ".config"),
                    ("XDG_DATA_HOME", ".local/share"),
                )
            )
        )

    def prepare(self) -> None:
        for directory in (self.state, self.config, self.data):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)

    def credential(self) -> str:
        path = self.config / "client.token"
        if not path.exists():
            private_write(path, secrets.token_urlsafe(32))
        path.chmod(0o600)
        return path.read_text().strip()


class WorkspaceLock:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self._file: IO[str] | None = None

    def acquire(self) -> None:
        self.workspace.prepare()
        self._file = (self.workspace.state / "owner.lock").open("a+")
        os.chmod(self._file.name, 0o600)
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._file.close()
            self._file = None
            raise AppError(
                "workspace_owned", "A service already owns this workspace", 409
            ) from error

    def release(self) -> None:
        if self._file:
            fcntl.flock(self._file, fcntl.LOCK_UN)
            self._file.close()
            self._file = None
