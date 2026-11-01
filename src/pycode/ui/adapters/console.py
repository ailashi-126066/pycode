# Source: author-owned mewcode/runtime/output.py.
# SHA-256: 1726aae4ccd94dc643956234638c60c2e148ad1215afc87bea6ddd40168bee71
# Adaptation: stream-json writes the 11.1 section 3.1 events with their envelope as-is.
"""Text, JSON and NDJSON presentations consume the common application events."""

from pycode.kernel import json_dumps, plain_data


class ConsoleOutput:
    def __init__(self, output_format, stream):
        self._format = output_format
        self._stream = stream

    def write_json(self, value):
        self._stream.write(json_dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        self._stream.flush()

    async def consume(self, event):
        if self._format == "stream-json":
            self.write_json({"type": event.kind, **plain_data(event)})
        elif event.kind == "subagent":
            # Only status changes; the subagent's own events stay in stream-json.
            if self._format == "text" and "event" not in event.data:
                self._stream.write(
                    f"[subagent] {event.data['name']} ({event.data['description'][:80]}): "
                    f"{event.data['status']}\n"
                )
                self._stream.flush()
        elif (
            self._format == "text"
            and event.kind == "tool_execution_update"
            and "delta" in event.data
        ):
            self._stream.write(event.data["delta"])
            self._stream.flush()
        elif event.kind == "result":
            payload = {"session_id": event.session_id, "run_id": event.run_id, **event.data}
            if self._format == "json":
                self.write_json(payload)
            else:
                self._stream.write(event.data["final_text"] + "\n")
                self._stream.write(f"Session: {event.session_id} · {event.data['outcome']}\n")
                totals = event.data["usage"]["main_turn"]["tokens"]

                def shown(name):
                    return str(totals[name]) if totals[name] is not None else "unavailable"

                self._stream.write(
                    f"Usage: input {shown('input_tokens')} · output {shown('output_tokens')}"
                    f" · total {shown('total_tokens')}\n"
                )
                if event.data["pending"]:
                    self.write_json({"needs_input": event.data["pending"]})
                self._stream.flush()

    def value(self, value):
        self.write_json(value)

    def usage(self, report):
        self._stream.write(
            f"Session: {report['session_id']} · run {report['run_id'] or 'unavailable'}\n"
        )
        for name in ("main_turn", "session"):
            group = report[name]
            tokens = group["tokens"]
            parts = []
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_miss_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "total_tokens",
            ):
                value = tokens[field]
                parts.append(f"{field}: {value if value is not None else 'unavailable'}")
            self._stream.write(
                f"{name} ({group['settled_requests']} requests): " + " · ".join(parts) + "\n"
            )
        for purpose, group in report["by_purpose"].items():
            value = group["tokens"]["total_tokens"]
            self._stream.write(
                f"{purpose}: {value if value is not None else 'unavailable'} tokens"
                f" · {group['settled_requests']} requests\n"
            )
        self._stream.flush()
