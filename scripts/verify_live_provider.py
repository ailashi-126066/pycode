"""Separate remote service acceptance from deterministic local protocol tests."""

import argparse
import asyncio
import json
import os
import re
from dataclasses import asdict
from pathlib import Path

from langchain_deepseek import ChatDeepSeek

from pycode.app import AppConfig, RunOptions, SessionResources, compose_application
from pycode.kernel import Message, ToolResult, ToolSpec
from pycode.models import ProviderConfig, ReasoningDeepSeek

ROOT = Path(__file__).resolve().parents[1]


class Journal:
    def __init__(self):
        self.messages = []

    async def append_message(self, message):
        self.messages.append(message)
        return message.id


class AuditConsumer:
    def __init__(self):
        self.kinds = []

    async def on_event(self, event):
        self.kinds.append(event.kind)


class AcceptanceProbe:
    spec = ToolSpec(
        name="AcceptanceProbe",
        description="Return the value for protocol acceptance verification.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        category="read",
        execution_mode="parallel",
    )

    def __init__(self, journal):
        self.journal = journal
        self.values = []

    async def execute(self, context, arguments):
        if self.journal.messages[-1].role != "assistant":
            raise AssertionError("Provider message was not committed before execution")
        self.values.append(arguments["value"])
        return ToolResult("", "", "Probe received: " + arguments["value"])


def load_credential(reference: str) -> bool:
    if os.environ.get(reference):
        return True
    path = ROOT / ".env"
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != reference:
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ[reference] = value
            return True
    return False


async def verify(args) -> int:
    report = {"provider": args.provider, "model": args.model, "acceptance": "remote_service"}
    if args.no_reasoning_passback:
        # Control run: send DeepSeek requests without the returned reasoning_content.
        ReasoningDeepSeek._get_request_payload = ChatDeepSeek._get_request_payload
        report["reasoning_passback"] = False
    reference = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }[args.provider]
    if not load_credential(reference):
        report.update(status="unverified", reason="credential_not_configured")
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("Remote service acceptance unverified: credential not configured")
        return 2
    provider = ProviderConfig(
        name=f"live-{args.provider}",
        implementation=args.provider,
        model_id=args.model,
        base_url=args.base_url,
        api_key_env=reference,
        context_window=32000,
        max_output_tokens=args.max_output,
        supports_tools=True,
        openai_api=args.api_mode if args.provider == "openai" else None,
        timeout_seconds=30,
        supports_thinking=args.thinking == "enabled",
        thinking={"type": "enabled"}
        if args.provider == "deepseek" and args.thinking == "enabled"
        else {},
    )
    config = AppConfig(ROOT, ROOT / ".acceptance" / args.provider)
    catalog = compose_application(config, (provider,)).model_catalog()
    await catalog.initialize()
    snapshot = await catalog.refresh()
    report["catalog_errors"] = snapshot.errors
    report["discovered_models"] = [
        model.model_id for model in snapshot.models if model.availability == "available"
    ]
    if not any(
        model.model_id == args.model and model.availability == "available"
        for model in snapshot.models
    ):
        report.update(status="unverified", reason="configured_model_not_discovered")
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("Remote acceptance unverified: configured model was not discovered")
        return 2
    journal = Journal()
    consumer = AuditConsumer()
    probe = AcceptanceProbe(journal)
    container = await compose_application(config).runtime(
        SessionResources(
            session_id=f"acceptance-{args.provider}",
            journal=journal,
            tools=(probe,),
            consumers=(consumer,),
        ),
        RunOptions(provider=provider, run_id=os.urandom(12).hex()),
    )
    try:
        result = await container.runtime.run(
            (
                Message(
                    id=os.urandom(12).hex(),
                    role="user",
                    text=(
                        "Call AcceptanceProbe with value 'pycode', then reply exactly OK. "
                        "Do not call any other tool."
                    ),
                ),
            ),
            container.context,
        )
        records = await container.request_store.list_requests(container.context.session_id)
        records = tuple(record for record in records if record.run_id == container.context.run_id)
        passed = (
            result.outcome == "completed"
            and probe.values == ["pycode"]
            and result.final_text.strip() == "OK"
        )
        report.update(
            status="passed" if passed else "failed",
            outcome=result.outcome,
            tool_values=probe.values,
            final_reply_ok=result.final_text.strip() == "OK",
            requests=[
                {
                    "request_id": record.request_id,
                    "status": record.status,
                    "purpose": record.purpose,
                    "error_code": record.error_code,
                    "usage": {
                        key: value for key, value in asdict(record.usage).items() if key != "raw"
                    }
                    if record.usage
                    else None,
                }
                for record in records
            ],
            event_kinds=consumer.kinds,
        )
        args.output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"Remote {args.provider} acceptance: {report['status']} ({len(records)} requests)")
        return 0 if passed else 1
    finally:
        await container.aclose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["deepseek", "openai", "anthropic"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--api-mode", choices=["chat_completions", "responses"], default="chat_completions"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--thinking", choices=["off", "enabled"], default="off")
    parser.add_argument("--max-output", type=int, default=256)
    parser.add_argument("--no-reasoning-passback", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_output <= 4096 or (
        args.thinking == "enabled" and args.provider != "deepseek"
    ):
        parser.error("Invalid output budget or unsupported live-test thinking configuration")
    args.output = args.output.resolve()
    if not args.output.is_relative_to(ROOT) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", args.output.name
    ):
        parser.error("Acceptance report must be written within this project")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    return asyncio.run(verify(args))


if __name__ == "__main__":
    raise SystemExit(main())
