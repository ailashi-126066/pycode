"""Non-interactive commands invoke the same public services as the UI."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from pycode.app.api import compose_application
from pycode.app.config import load_providers
from pycode.kernel import DomainError, Message, RunResult, plain_data


def leaf_id(value):
    return None if value == "root" else value


def read_providers(path):
    return load_providers(Path(path).expanduser().resolve())


def read_json(path):
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        raise DomainError("invalid_json_file", "Cannot read UTF-8 JSON file") from error


def read_skill_source(path):
    try:
        with Path(path).expanduser().open("rb") as source:
            data = source.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            raise ValueError("Skill exceeds 256 KiB")
        return data.decode("utf-8-sig")
    except (OSError, UnicodeError, ValueError) as error:
        raise DomainError("invalid_skill_file", "Cannot read bounded UTF-8 Skill file") from error


async def command(args, config):
    output = compose_application(config).console(args.output_format)
    name = args.command.removeprefix("/")
    local_handler = _LOCAL_COMMANDS.get(name)
    if local_handler is not None:
        return await local_handler(args, config, output)
    if name == "memory" and args.providers is None:
        return await local_memory_command(args, config, output)
    if args.providers is None:
        raise DomainError("providers_required", "Execution requires --providers")
    providers = await asyncio.to_thread(read_providers, args.providers)
    if args.session is None and args.command not in {"run", "plan", "/plan"}:
        raise DomainError("session_required", "This command requires --session")
    if args.create and args.command not in {"run", "plan", "/plan", "hooks"}:
        raise DomainError("invalid_create_option", "Only run can create a session")
    session_id = args.session or uuid4().hex
    session = await compose_application(config, providers).open_session(
        session_id,
        create=args.create or args.session is None,
        consumers=(output,),
        refresh_models=not args.cached if args.command == "run" else False,
        startup_hooks=args.command != "hooks",
        connect_mcp=args.command != "hooks",
        # Nobody can answer a subagent's approval here (11.1 section 36 Q2).
        interactive=False,
    )
    try:
        handler = _EXECUTION_COMMANDS.get(name)
        if handler is None:
            raise DomainError("unknown_command", "Unknown execution command")
        result = await handler(args, session, output)
        if isinstance(result, int):
            return result
        return await finish_execution(session, output, result)
    except asyncio.CancelledError:
        await publish_cancelled(session)
        raise
    finally:
        await session.aclose()


async def storage_command(args, config, output):
    storage = compose_application(config).storage()
    if args.storage_command == "status":
        value = await storage.status()
    elif args.storage_command == "gc":
        value = await storage.collect(dry_run=args.dry_run)
    else:
        # Deletion cannot be undone, so it is confirmed first.
        question = f"Delete session {args.session_id}? [y/N] "
        confirmed = args.yes or await asyncio.to_thread(input, question) == "y"
        if confirmed:
            await storage.delete_session(args.session_id)
        value = {"session_id": args.session_id, "deleted": confirmed}
    output.value(value)
    return 0


async def local_memory_command(args, config, output):
    if args.session is None:
        raise DomainError("session_required", "Memory commands require --session")
    repository = compose_application(config).session_repository()
    store = await repository.open(args.session)
    memory = await compose_application(config).memory_service(store)
    try:
        if args.memory_command == "list":
            value = await memory.list()
        elif args.memory_command == "mode":
            await memory.set_mode(args.mode)
            value = {"mode": args.mode}
        elif args.memory_command in {"remove", "delete"}:
            await memory.remove(args.memory_id, scope=args.scope)
            value = {"removed": args.memory_id}
        elif args.memory_command == "edit":
            value = {"memory_id": await memory.edit(args.memory_id, args.content, scope=args.scope)}
        elif args.memory_command == "clear":
            await memory.clear()
            value = {"cleared": True}
        else:
            raise DomainError("providers_required", "Memory extraction requires --providers")
        output.value(value)
        return 0
    finally:
        try:
            await memory.aclose()
        finally:
            await store.aclose()


async def execute_run(args, session, output):
    if not args.prompt.strip():
        raise DomainError("empty_prompt", "Prompt must not be empty")
    return await session.run(Message(args.input_id or uuid4().hex, "user", args.prompt))


async def execute_plan(args, session, output):
    planned = await session.plans.enter()
    if args.task:
        return await session.run(Message(uuid4().hex, "user", args.task))
    if not isinstance(planned, RunResult):
        output.value(plain_data(planned))
        return 0
    return planned


async def execute_resume(args, session, output):
    if args.answers_file:
        answers = read_json(args.answers_file)
    elif args.answers:
        answers = json.loads(args.answers)
    else:
        output.value(await session.inspect())
        return 0
    return await session.resume(answers)


async def execute_compact(args, session, output):
    output.value(plain_data(await session.compact()))
    return 0


async def execute_branch(args, session, output):
    await session.branch(leaf_id(args.leaf))
    output.value(await session.inspect())
    return 0


async def execute_skill(args, session, output):
    if args.skill_command == "load":
        request = await session.skills.load(args.name, args.arguments)
        return await session.run(
            Message(
                uuid4().hex,
                "user",
                f"Use skill {args.name} at {request['path']}. "
                f"Read its SKILL.md and follow its instructions.\nTask arguments: {args.arguments}",
                provider_metadata={"skill_request": request},
            )
        )
    else:
        if args.skill_command == "reload":
            value = await session.skills.refresh()
        elif args.skill_command == "install":
            value = await session.skills.install(args.url, ref=args.ref, subpath=args.subpath)
        elif args.skill_command == "read":
            value = plain_data(await session.skills.read(args.name))
        elif args.skill_command in {"create", "update"}:
            content = await asyncio.to_thread(read_skill_source, args.file)
            write = (
                session.skills.create if args.skill_command == "create" else session.skills.update
            )
            value = await write(args.name, content, scope=args.scope)
        else:
            value = await session.skills.search(args.query, args.limit)
        output.value(value)
        return 0


async def execute_memory(args, session, output):
    if args.memory_command == "list":
        value = await session.memory.list()
    elif args.memory_command == "mode":
        await session.memory.set_mode(args.mode)
        value = {"mode": args.mode}
    elif args.memory_command in {"remove", "delete"}:
        await session.memory.remove(args.memory_id, scope=args.scope)
        value = {"removed": args.memory_id}
    elif args.memory_command == "edit":
        value = {
            "memory_id": await session.memory.edit(args.memory_id, args.content, scope=args.scope)
        }
    elif args.memory_command == "clear":
        await session.memory.clear()
        value = {"cleared": True}
    else:
        value = await session.memory.extract()
    output.value(value)
    return 0


async def finish_execution(session, output, result):
    # Background subagents report back as a new run once the main run has finished.
    while result.outcome == "completed":
        await session.wait_subagents()
        if not session.wake_pending():
            break
        await session.publish_result(result)
        next_result = await session.wake()
        if next_result is None:
            break
        result = next_result
    await session.publish_result(result, collect_background=True)
    return 0 if result.outcome == "completed" else 2 if result.outcome == "needs_input" else 1


async def publish_cancelled(session):
    state = await session.inspect()
    run_id = state["pending_run_id"]
    if run_id is None:
        for entry in reversed(state["snapshot"]["entries"]):
            if entry["data"].get("kind") == "run_end":
                run_id = entry["run_id"]
                break
    if run_id is not None:
        await session.publish_result(
            RunResult("aborted", "", (), run_id=run_id), collect_background=True
        )


async def revert_command(args, session, output):
    async with session.execution():
        record = await session.snapshots.revert(leaf_id(args.target), args.mode)
        await session.plans.initialize()
        await session.tasks.initialize()
    output.value({"revert": plain_data(record), "session": await session.inspect()})
    return 0


async def view_command(args, config, output):
    if args.command in {"session", "/session"}:
        manager = compose_application(config).session_service()
        if args.session_command == "list":
            output.value({"sessions": await manager.list_sessions()})
            return 0
        if args.session_command == "name":
            output.value(await manager.rename(args.session_id, args.title))
            return 0
        session_id = await manager.resolve(args.session_id)
    else:
        session_id = args.session
    view = await compose_application(config).open_view(session_id)
    try:
        if args.command in {"usage", "/usage"}:
            report = await view.usage(args.run)
            if args.json or args.output_format != "text":
                output.value(report)
            else:
                output.usage(report)
        else:
            state = await view.inspect()
            if args.session_command == "fork":
                target = state["snapshot"]["leaf_id"] if args.leaf is None else leaf_id(args.leaf)
                output.value(await manager.fork(session_id, target, args.target_id))
            elif args.session_command == "tree":
                output.value(state["snapshot"])
            else:
                output.value(state)
    finally:
        await view.aclose()
    return 0


_LOCAL_COMMANDS = {
    "storage": storage_command,
    "usage": view_command,
    "session": view_command,
}

_EXECUTION_COMMANDS = {
    "run": execute_run,
    "plan": execute_plan,
    "resume": execute_resume,
    "compact": execute_compact,
    "branch": execute_branch,
    "revert": revert_command,
    "skill": execute_skill,
    "memory": execute_memory,
}
