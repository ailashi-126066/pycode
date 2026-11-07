"""`pycode` opens the TUI. Everything else happens inside it, through slash commands."""

import argparse
from pathlib import Path

from ...config import Settings
from .app import PycodeApp


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="pycode", description="Terminal coding agent")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Project directory")
    parser.add_argument("--providers", type=Path, help="Provider JSON (default: providers.json)")
    parser.add_argument("--provider", help="Provider name to use (default: the first one)")
    parser.add_argument("--session", help="Open this session")
    parser.add_argument(
        "--max-turns",
        type=int,
        default=100,
        help="Most model calls before a run stops; counting restarts after each approval",
    )
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    settings = Settings(
        workspace=workspace,
        providers_file=args.providers or workspace / "providers.json",
        provider_name=args.provider,
        max_turns=args.max_turns,
    )
    PycodeApp(settings, args.session).run()
