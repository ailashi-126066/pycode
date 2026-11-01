"""pycode command entry point."""

import asyncio
import io
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from pycode.app.api import compose_application
from pycode.app.cli.commands import command
from pycode.app.cli.parser import build_parser
from pycode.app.config import load_config, load_providers
from pycode.kernel import DomainError, OutputClosed, json_dumps, plain_data
from pycode.sandbox import SandboxSettings


def main(argv: list[str] | None = None) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper) and sys.stdout.encoding.lower() not in {
        "utf-8",
        "utf8",
    }:
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    config = replace(
        load_config(args.workspace, args.data_dir),
        sandbox=SandboxSettings(args.sandbox, args.sandbox_network),
        project_trusted=args.trust_project,
        memory_mode=args.memory_mode,
        max_turns=args.max_turns,
        max_agents=args.max_agents,
        subprocess_passthrough=tuple(args.pass_env),
    )
    if args.p is not None:
        if args.command is not None:
            parser.error("-p cannot be combined with a subcommand")
        args.command, args.prompt = "run", args.p
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "tui":
        try:
            providers = load_providers(Path(args.providers).expanduser().resolve())
            compose_application(config, providers).tui(session_id=args.session).run()
            return 0
        except DomainError as error:
            print(
                json_dumps(
                    {"error": {"code": error.code, "message": str(error)}}, ensure_ascii=False
                )
            )
            return 1
    if args.command in {"model", "/model"}:
        try:
            providers = load_providers(Path(args.providers).expanduser().resolve())
            catalog = compose_application(config, providers).model_catalog()
            return asyncio.run(model_command(args, config, catalog, providers))
        except DomainError as error:
            print(
                json_dumps(
                    {"error": {"code": error.code, "message": str(error)}}, ensure_ascii=False
                )
            )
            return 1
    if args.command != "doctor":
        try:
            if args.command in {"compact", "/compact"} and args.keep_recent_tokens is not None:
                if args.keep_recent_tokens < 1:
                    raise DomainError(
                        "invalid_summary_config", "Recent token retention must be positive"
                    )
                config = replace(config, summary_keep_recent_tokens=args.keep_recent_tokens)
            return asyncio.run(command(args, config))
        except DomainError as error:
            print(
                json_dumps(
                    {"type": "error", "error": {"code": error.code, "message": str(error)}},
                    ensure_ascii=False,
                )
            )
            return 1
        except KeyboardInterrupt:
            return 130
        except OutputClosed:
            return 141
        except BrokenPipeError:
            return 141
    application = compose_application(config)
    status = application.status()
    isolation = asyncio.run(application.sandbox_status())
    tools = external_tools()
    if args.json:
        print(
            json_dumps(
                {**plain_data(status), "sandbox": plain_data(isolation), "tools": tools},
                ensure_ascii=False,
            )
        )
    else:
        print(f"{status.name} {status.version}")
        print(f"Workspace: {status.workspace.path}")
        availability = (
            "available" if status.workspace.snapshot_available else "unavailable (non-Git)"
        )
        print(f"File snapshots: {availability}")
        print(isolation.reason)
        for tool in tools:
            if tool["path"]:
                print(f"{tool['name']}: {tool['path']}")
            else:
                print(f"{tool['name']}: missing ({tool['purpose']}); {tool['install']}")
    return 0 if status.workspace.exists else 1


def external_tools():
    # Missing tools are reported, not fatal: each only disables the feature that needs it.
    darwin = sys.platform == "darwin"
    required = [
        (
            "rg",
            "glob and grep search tools",
            "brew install ripgrep" if darwin else "install ripgrep and put rg on PATH",
        ),
        (
            "git",
            "file snapshots and revert",
            "xcode-select --install or brew install git" if darwin else "install git",
        ),
        ("bash", "Bash tool and command hooks", "install bash and put it on PATH"),
    ]
    if darwin:
        required.append(
            ("/usr/bin/sandbox-exec", "Seatbelt sandbox", "ships with macOS; restore the OS file")
        )
    return [
        {"name": name, "path": shutil.which(name), "purpose": purpose, "install": install}
        for name, purpose, install in required
    ]


async def model_command(args, config, catalog, providers) -> int:
    if args.model_command == "select":
        if not args.session:
            raise DomainError("session_required", "Selecting a model requires an active session")
        session = await compose_application(config, providers).open_session(
            args.session, refresh_models=not args.cached
        )
        try:
            selected = await session.select_model(args.provider, args.model_id)
            print(
                json_dumps(
                    {"selection": plain_data(selected), "session_id": args.session},
                    ensure_ascii=False,
                )
            )
            return 0
        finally:
            await session.aclose()
    store = None
    if args.session:
        store = await compose_application(config).session_repository().open(args.session)
    control = compose_application(config, providers).model_control(store, catalog=catalog)
    try:
        snapshot = await control.list_models(refresh=not args.cached)
        current = await control.current()
        if args.json:
            print(
                json_dumps(
                    {
                        "catalog": plain_data(snapshot),
                        "selection": plain_data(current) if current else None,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            for model in snapshot.models:
                print(f"{model.provider}/{model.model_id} [{model.availability}]")
            if current is not None:
                print(
                    f"Current: {current.provider}/{current.model_id} · thinking {current.thinking}"
                )
            for provider, code in snapshot.errors.items():
                print(f"{provider}: {code}")
        return 1 if snapshot.errors else 0
    finally:
        if store is not None:
            await store.aclose()
