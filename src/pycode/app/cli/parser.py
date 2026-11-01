"""Explicit commands keep execution, navigation and file restoration distinct."""

import argparse

from pycode import __version__
from pycode.sandbox import passthrough_name


def execution_options(parser, *, suppress=False):
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--providers", default=default, help="Provider configuration JSON file")
    parser.add_argument("--session", default=default, help="Native session ID")
    parser.add_argument(
        "--cached", action="store_true", default=default, help="Skip model discovery"
    )
    parser.add_argument(
        "--output-format",
        choices=("text", "json", "stream-json"),
        default=argparse.SUPPRESS if suppress else "text",
    )
    parser.add_argument("--input-id", default=default, help="Stable input ID for retry")
    parser.add_argument(
        "--create", action="store_true", default=default, help="Create named session"
    )


def build_parser():
    parser = argparse.ArgumentParser(prog="pycode", description="pycode coding assistant")
    parser.add_argument("--version", action="version", version=f"pycode {__version__}")
    parser.add_argument("--workspace", help="Project directory")
    parser.add_argument("--data-dir", help="User configuration and storage directory")
    parser.add_argument("--memory-mode", choices=("auto", "off"))
    parser.add_argument("--max-agents", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--max-turns", type=int, default=100, help="Turns before a run stops")
    parser.add_argument("--team-idle-timeout", type=float, default=180)
    parser.add_argument("--team-stop-timeout", type=float, default=15)
    parser.add_argument("--team-max-handoffs", type=int, default=1)
    parser.add_argument(
        "--pass-env",
        action="append",
        default=[],
        type=passthrough_name,
        metavar="NAME",
        help="Pass one exact non-credential environment variable to child processes; repeatable",
    )
    parser.add_argument(
        "--trust-project", action="store_true", help="Enable this project's Skill sources"
    )
    parser.add_argument(
        "--sandbox",
        choices=("off", "on", "on-auto"),
        default="off",
        help="macOS command sandbox mode; other hosts retain approval",
    )
    parser.add_argument(
        "--sandbox-network", action="store_true", help="Allow network in an enabled macOS sandbox"
    )
    parser.add_argument("-p", metavar="PROMPT", help="Run a prompt non-interactively")
    execution_options(parser)
    commands = parser.add_subparsers(dest="command")
    tui = commands.add_parser("tui", help="Open the interactive terminal UI")
    tui.add_argument("--providers", required=True)
    tui.add_argument("--session")
    doctor = commands.add_parser("doctor", help="Inspect workspace capabilities")
    doctor.add_argument("--json", action="store_true")
    model = commands.add_parser("model", aliases=["/model"], help="List or select models")
    model.add_argument("--providers", required=True)
    model.add_argument("--cached", action="store_true")
    model.add_argument("--json", action="store_true")
    model.add_argument("--session")
    sub = model.add_subparsers(dest="model_command")
    sub.add_parser("list")
    name = sub.add_parser("name")
    name.add_argument("session_id")
    name.add_argument("title")
    opened = sub.add_parser("open")
    opened.add_argument("session_id")
    choose = sub.add_parser("select")
    choose.add_argument("provider")
    choose.add_argument("model_id")
    run = commands.add_parser("run", help="Run a user task")
    execution_options(run, suppress=True)
    run.add_argument("prompt")
    plan = commands.add_parser(
        "plan", aliases=["/plan"], help="Enter read-only planning and review its Markdown"
    )
    execution_options(plan, suppress=True)
    plan.add_argument("task", nargs="?", default="")
    resume = commands.add_parser("resume", help="Open saved history; old execution waits expire")
    execution_options(resume, suppress=True)
    answers = resume.add_mutually_exclusive_group()
    answers.add_argument("--answers", help="Answer object as JSON")
    answers.add_argument("--answers-file", help="UTF-8 JSON answer file")
    compact = commands.add_parser("compact", aliases=["/compact"], help="Compact current context")
    execution_options(compact, suppress=True)
    compact.add_argument(
        "--keep-recent-tokens", type=int, help="Recent context retention for this compaction"
    )
    branch = commands.add_parser("branch", help="Navigate dialogue without restoring files")
    execution_options(branch, suppress=True)
    branch.add_argument("leaf", help="Entry ID or root")
    skill = commands.add_parser("skill", help="Search, load or refresh the session Skill catalog")
    execution_options(skill, suppress=True)
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    search = skill_commands.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--limit", type=int, default=10)
    skill_commands.add_parser("reload")
    load = skill_commands.add_parser("load")
    load.add_argument("name")
    load.add_argument("arguments", nargs="?", default="")
    install = skill_commands.add_parser("install")
    install.add_argument("url")
    install.add_argument("--ref")
    install.add_argument("--path", dest="subpath")
    read_skill = skill_commands.add_parser("read", help="Read a Skill and its revision")
    read_skill.add_argument("name")
    for operation in ("create", "update"):
        author = skill_commands.add_parser(operation, help=f"{operation.title()} a local Skill")
        author.add_argument("name")
        author.add_argument("--file", required=True, help="UTF-8 SKILL.md source file")
        author.add_argument("--scope", choices=("user", "project"), default="user")
        if operation == "update":
            author.add_argument("--revision", required=True, help="Current SHA-256 revision")
    memory = commands.add_parser(
        "memory", aliases=["/memory"], help="Inspect and edit durable JSON memory"
    )
    execution_options(memory, suppress=True)
    memory_actions = memory.add_subparsers(dest="memory_command", required=True)
    memory_actions.add_parser("list")
    memory_actions.add_parser("clear")
    memory_actions.add_parser("extract")
    edit_memory = memory_actions.add_parser("edit")
    edit_memory.add_argument("memory_id")
    edit_memory.add_argument("content")
    # --scope picks the scope that holds MEMORY_ID when both scopes use the same ID.
    edit_memory.add_argument("--scope", choices=["user", "project"])
    mode = memory_actions.add_parser("mode")
    mode.add_argument("mode", choices=("auto", "off"))
    remove = memory_actions.add_parser("remove", aliases=["delete"])
    remove.add_argument("memory_id")
    remove.add_argument("--scope", choices=["user", "project"])
    memory_actions.add_parser("undo", help="Undo the latest automatic memory update")
    revert = commands.add_parser("revert", help="Preview, apply or recover an explicit revert")
    execution_options(revert, suppress=True)
    revert_commands = revert.add_subparsers(dest="revert_command", required=True)
    preview = revert_commands.add_parser("preview")
    preview.add_argument("target", help="Entry ID or root")
    preview.add_argument("--mode", choices=("dialogue", "code", "both"), default="dialogue")
    preview.add_argument("--plan-out", help="Write UTF-8 plan to a new file")
    apply = revert_commands.add_parser("apply")
    apply.add_argument("--plan-file", required=True)
    recover = revert_commands.add_parser("recover")
    recover.add_argument("transaction_id")
    recover.add_argument("--compensate", action="store_true")
    usage = commands.add_parser(
        "usage", aliases=["/usage"], help="Inspect nullable token accounting"
    )
    usage.add_argument("--session", required=True)
    usage.add_argument("--run")
    usage.add_argument("--json", action="store_true")
    sessions = commands.add_parser(
        "session", aliases=["/session"], help="List, inspect or fork sessions"
    )
    storage = commands.add_parser(
        "storage", aliases=["/storage"], help="Inspect and collect managed objects"
    )
    actions = storage.add_subparsers(dest="storage_command", required=True)
    actions.add_parser("status")
    collect = actions.add_parser("gc")
    collect.add_argument("--dry-run", action="store_true")
    for action in ("delete-session", "restore-session"):
        item = actions.add_parser(action)
        item.add_argument("session_id")
    sub = sessions.add_subparsers(dest="session_command", required=True)
    worktrees = commands.add_parser("worktree", help="Inspect and confirm explicit integration")
    worktree_actions = worktrees.add_subparsers(dest="worktree_command", required=True)
    inspect_worktree = worktree_actions.add_parser("inspect")
    inspect_worktree.add_argument("worktree_id")
    confirm_worktree = worktree_actions.add_parser("confirm-integration")
    confirm_worktree.add_argument("worktree_id")
    confirm_worktree.add_argument("--validated", action="store_true", required=True)
    confirm_worktree.add_argument("--worker-stopped", action="store_true", required=True)
    worktree_actions.add_parser("scan")
    sub.add_parser("list")
    for name in ("show", "tree"):
        view = sub.add_parser(name)
        view.add_argument("session_id")
    fork = sub.add_parser("fork")
    fork.add_argument("session_id")
    fork.add_argument("target_id")
    fork.add_argument("--leaf", help="Entry ID or root; default current leaf")
    return parser
