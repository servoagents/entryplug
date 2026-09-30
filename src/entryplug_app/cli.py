"""Mission argparse extensions; optional imports happen only on execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from entryplug_app.contracts import AppError, load_definition
from entryplug_app.workspace import Workspace, private_write


def register(commands: Any) -> None:
    def common(parser: argparse.ArgumentParser) -> None:
        parser.set_defaults(application_command=True)
        parser.add_argument(
            "--workspace", default="default", help="XDG workspace name or explicit directory"
        )
        parser.add_argument("--json", action="store_true")

    for name in ("serve", "ui"):
        parser = commands.add_parser(
            name, help="Run the mission service" if name == "serve" else "Open the mission console"
        )
        common(parser)
        parser.add_argument("--port", type=int, default=8765)
        parser.add_argument(
            "--no-demo", action="store_true", help="Disable the explicitly simulated body"
        )
    parser = commands.add_parser("auth", help="Manage service-owned inference profiles")
    subs = parser.add_subparsers(dest="verb", required=True)
    for name in ("login", "logout", "list", "configure", "models"):
        child = subs.add_parser(name)
        common(child)
        child.add_argument("--profile", default="personal-chatgpt")
        if name == "login":
            child.add_argument("provider", choices=["chatgpt"])
        if name == "configure":
            child.add_argument("--provider", choices=["chatgpt", "openai"], required=True)
            child.add_argument("--model", required=True)
            child.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser = commands.add_parser("mission", help="Validate and control resident missions")
    subs = parser.add_subparsers(dest="verb", required=True)
    for name in (
        "validate",
        "create",
        "start",
        "list",
        "status",
        "tail",
        "instruct",
        "pause",
        "resume",
        "stop",
        "export",
        "update",
    ):
        child = subs.add_parser(name)
        common(child)
        child.add_argument("--key", help="Stable idempotency key for a retry")
        if name in {"validate", "create"}:
            child.add_argument("file", type=Path)
        elif name != "list":
            child.add_argument("id")
        if name in {"instruct", "update"}:
            child.add_argument("--file", required=True, type=Path)
        if name == "update":
            child.add_argument("--revision", required=True, type=int)
            child.add_argument("--confirm-access-change", action="store_true")
        if name == "tail":
            child.add_argument("--after", type=int, default=None)
    for group, verb in (
        ("body", "list"),
        ("agent", "list"),
        ("operation", "inspect"),
        ("alert", "list"),
    ):
        parser = commands.add_parser(group)
        subs = parser.add_subparsers(dest="verb", required=True)
        child = subs.add_parser(verb)
        common(child)
        if group == "operation":
            child.add_argument("id")
        if group == "alert":
            child = subs.add_parser("ack")
            common(child)
            child.add_argument("id")
    parser = commands.add_parser("topology", help="Read the observed/configured body topology")
    common(parser)
    parser = commands.add_parser("demo", help="Advance the labelled SIMULATED camera fixture")
    common(parser)
    parser.add_argument(
        "action",
        choices=(
            "idle",
            "enter_a",
            "presence_a",
            "enter_b",
            "exit_a",
            "lost",
            "recover",
            "restart",
            "stale",
            "sequence",
        ),
    )
    parser = commands.add_parser(
        "service", help="Explicit systemd user-service installation and control"
    )
    subs = parser.add_subparsers(dest="verb", required=True)
    for name in ("install", "start", "status", "stop"):
        child = subs.add_parser(name)
        common(child)
        child.add_argument("--user", action="store_true")
        child.add_argument("--port", type=int, default=8765)


def output(value: Any, structured: bool) -> None:
    if structured:
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    elif isinstance(value, list):
        for item in value:
            output(item, False)
    elif isinstance(value, dict):
        print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        print(value)


async def _client_command(args: argparse.Namespace) -> Any:
    from entryplug_server.client import EntryplugClient

    async with EntryplugClient.from_workspace(args.workspace) as client:
        if args.command == "auth":
            if args.verb == "list":
                return await client.request("GET", "profiles")
            if args.verb == "models":
                return await client.request("GET", f"profiles/{args.profile}/models")
            if args.verb == "configure":
                return await client.request(
                    "POST",
                    "profiles",
                    {
                        "id": args.profile,
                        "provider": args.provider,
                        "model": args.model,
                        "api_key_env": args.api_key_env,
                    },
                )
            if args.verb == "logout":
                return await client.request("POST", "auth/logout", {"profile": args.profile})
            result = await client.request("POST", "auth/chatgpt/start", {"profile": args.profile})
            await asyncio.to_thread(webbrowser.open, result["authorization_url"])
            return {
                "profile": args.profile,
                "status": "sign_in_opened",
                "next": "Complete browser sign-in, then select a model with auth configure",
            }
        if args.command == "mission":
            verb = args.verb
            if verb == "create":
                return await client.create(load_definition(args.file).to_dict(), key=args.key)
            if verb == "start":
                return await client.start(args.id, key=args.key)
            if verb == "list":
                return await client.request("GET", "missions")
            if verb == "status":
                return await client.request("GET", "runs/" + args.id)
            if verb == "export":
                return (await client.request("GET", "missions/" + args.id))["definition"]
            if verb == "update":
                return await client.request(
                    "PATCH",
                    "missions/" + args.id,
                    {
                        "definition": load_definition(args.file).to_dict(),
                        "confirm_access_change": args.confirm_access_change,
                    },
                    revision=args.revision,
                    key=args.key,
                )
            if verb == "tail":
                snapshot = await client.snapshot()
                cursor = snapshot["cursor"] if args.after is None else args.after
                async for event in client.events(after=cursor, run_id=args.id):
                    output(event, args.json)
                return None
            payload = (
                {"text": args.file.read_text(encoding="utf-8"), "target": "next_turn"}
                if verb == "instruct"
                else {}
            )
            return await client.request(
                "POST",
                f"runs/{args.id}/{'inputs' if verb == 'instruct' else verb}",
                payload,
                key=args.key,
            )
        if args.command == "demo":
            from entryplug_app.demo import SEQUENCE

            if args.action == "sequence":
                for action in SEQUENCE:
                    output(await client.request("POST", "demo/step", {"action": action}), args.json)
                    await asyncio.sleep(0.1)
                return None
            return await client.request("POST", "demo/step", {"action": args.action})
        if args.command == "alert" and args.verb == "ack":
            return await client.request("POST", f"alerts/{args.id}/ack", {})
        path = {"body": "bodies", "agent": "agents", "topology": "topology", "alert": "alerts"}.get(
            args.command
        )
        return await client.request("GET", path or "operations/" + args.id)


async def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from entryplug_app.service import ApplicationService
    from entryplug_server.app import create_app
    from entryplug_server.client import EntryplugClient
    from entryplug_server.security import LocalSecurity

    if args.command == "ui":
        try:
            async with EntryplugClient.from_workspace(args.workspace) as client:
                await client.request("GET", "health")
                bootstrap = await client.request("POST", "session/bootstrap", {})
                url = (
                    str(client.http.base_url).rstrip("/") + "/#bootstrap=" + bootstrap["bootstrap"]
                )
                await asyncio.to_thread(webbrowser.open, url)
                print("Opened the existing mission service.", file=sys.stderr)
                return
        except AppError as error:
            if error.code != "service_not_running":
                raise
    if not 1024 <= args.port <= 65535:
        raise AppError("validation_error", "Choose a port from 1024 through 65535")
    workspace = Workspace.resolve(args.workspace)
    service = ApplicationService(workspace, demo=not args.no_demo)
    await service.open()
    base_url = f"http://127.0.0.1:{args.port}"
    security = LocalSecurity(workspace.credential(), base_url)
    try:
        app = create_app(
            service, base_url=base_url, security=security, manage_lifespan=False, close_service=True
        )
        private_write(
            workspace.state / "service.json",
            json.dumps(
                {
                    "base_url": base_url,
                    "pid": os.getpid(),
                    "workspace_id": service.store.workspace_id,
                }
            ),
        )
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            workers=1,
            access_log=False,
            log_level="warning",
            timeout_graceful_shutdown=10,
        )
        server = uvicorn.Server(config)

        async def open_when_ready() -> None:
            while not server.started and not server.should_exit:
                await asyncio.sleep(0.05)
            if server.started:
                await asyncio.to_thread(
                    webbrowser.open, base_url + "/#bootstrap=" + security.bootstrap()
                )

        opener = asyncio.create_task(open_when_ready()) if args.command == "ui" else None
        print(
            f"Entryplug service at {base_url}; Ctrl-C closes the owner. "
            "Use entryplug ui to open the console.",
            file=sys.stderr,
        )
        try:
            await server.serve()
        finally:
            if opener:
                opener.cancel()
                await asyncio.gather(opener, return_exceptions=True)
    finally:
        (workspace.state / "service.json").unlink(missing_ok=True)
        await service.close()


def _systemd(args: argparse.Namespace) -> int:
    from hashlib import sha256

    workspace_name = (
        str(Path(args.workspace).expanduser().resolve())
        if "/" in args.workspace or args.workspace.startswith(".")
        else args.workspace
    )
    unit = "entryplug-" + sha256(workspace_name.encode()).hexdigest()[:12] + ".service"
    if args.verb == "install":
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        path = config / "systemd" / "user" / unit

        def quote(value: str) -> str:
            return (
                '"'
                + value.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("%", "%%")
                .replace("$", "$$")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                + '"'
            )

        command = " ".join(
            quote(v)
            for v in [
                sys.executable,
                "-m",
                "entryplug",
                "serve",
                "--workspace",
                workspace_name,
                "--port",
                str(args.port),
            ]
        )
        private_write(
            path,
            "[Unit]\nDescription=Entryplug mission service\nAfter=network.target\n\n"
            "[Service]\nType=simple\nExecStart="
            + command
            + "\nRestart=on-failure\nRestartSec=5\nUMask=0077\n\n"
            "[Install]\nWantedBy=default.target\n",
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        output(
            {
                "unit": unit,
                "path": str(path),
                "note": "Installed, not enabled. User logout behavior depends on systemd linger.",
            },
            args.json,
        )
        return 0
    return subprocess.run(["systemctl", "--user", args.verb, unit], check=False).returncode


def run(args: argparse.Namespace) -> int:
    try:
        if args.command == "mission" and args.verb == "validate":
            output({"valid": True, "definition": load_definition(args.file).to_dict()}, args.json)
        elif args.command in {"serve", "ui"}:
            asyncio.run(_serve(args))
        elif args.command == "service":
            return _systemd(args)
        else:
            result = asyncio.run(_client_command(args))
            if result is not None:
                output(result, args.json)
        return 0
    except KeyboardInterrupt:
        return 0
    except ImportError as error:
        print(
            f"Optional dependency missing ({error.name}); "
            "install entryplug[ui] for the console/service.",
            file=sys.stderr,
        )
        return 2
    except AppError as error:
        print(
            json.dumps({"code": error.code, "message": str(error), "retryable": error.retryable}),
            file=sys.stderr,
        )
        return (
            4
            if error.status == 503
            else 3
            if error.status in {409, 412}
            else 5
            if error.status >= 500
            else 2
        )
    except (OSError, ValueError) as error:
        print(f"entryplug: {error}", file=sys.stderr)
        return 2
