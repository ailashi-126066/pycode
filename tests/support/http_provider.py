"""Real loopback HTTP transport for public SDK and LangChain protocol tests."""

import json
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class HTTPResponse:
    body: bytes
    status: int = 200
    content_type: str = "text/event-stream"
    headers: dict[str, str] = field(default_factory=dict)
    started: threading.Event | None = None
    release: threading.Event | None = None


@contextmanager
def protocol_server(responses):
    pending = None if callable(responses) else deque(responses)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            request = {"method": "POST", "path": self.path, "body": json.loads(data)}
            requests.append(request)
            self.reply(request)

        def do_GET(self):
            request = {"method": "GET", "path": self.path, "body": None}
            requests.append(request)
            self.reply(request)

        def reply(self, request):
            if callable(responses):
                response = responses(request)
            else:
                if not pending:
                    self.send_error(500, "No response fixture remains")
                    return
                response = pending.popleft()
            if response.started is not None:
                response.started.set()
            if response.release is not None:
                if not response.release.wait(timeout=10):
                    self.send_error(504, "Delayed fixture was not released")
                    return
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(response.body)
                self.wfile.flush()
            except ConnectionError:
                # Cancellation tests deliberately close the real SDK connection.
                return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


def chat_completion(*, tool=False, deepseek=False):
    base = {
        "id": "provider-1" if tool else "provider-2",
        "model": "wire-model",
        "object": "chat.completion.chunk",
        "created": 1,
    }
    chunks = []

    def delta(content, finish=None):
        chunks.append(
            {**base, "choices": [{"index": 0, "delta": content, "finish_reason": finish}]}
        )

    delta({"role": "assistant"})
    if deepseek:
        delta({"reasoning_content": "Thinking from provider"})
    delta({"content": "Checking" if tool else "done"})
    if tool:
        delta(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"value":"wire"}'},
                    }
                ]
            }
        )
    delta({}, "tool_calls" if tool else "stop")
    usage = {
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "total_tokens": 25,
        "completion_tokens_details": {"reasoning_tokens": 3},
    }
    if deepseek:
        usage.update(prompt_cache_hit_tokens=12, prompt_cache_miss_tokens=8)
    else:
        usage["prompt_tokens_details"] = {"cached_tokens": 12}
    chunks.append({**base, "choices": [], "usage": usage})
    content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    return HTTPResponse((content + "data: [DONE]\n\n").encode())


def anthropic_message(*, tool=False):
    events = []
    events.append(
        {
            "type": "message_start",
            "message": {
                "id": "provider-1" if tool else "provider-2",
                "type": "message",
                "role": "assistant",
                "model": "wire-model",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 12,
                    "cache_creation_input_tokens": 2,
                },
            },
        }
    )
    events.append(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        }
    )
    events.append(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Thinking from provider"},
        }
    )
    events.append(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "provider-signature"},
        }
    )
    events.append({"type": "content_block_stop", "index": 0})
    events.append(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": "Checking" if tool else "done"},
        }
    )
    events.append({"type": "content_block_stop", "index": 1})
    if tool:
        events.append(
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "echo",
                    "input": {},
                },
            }
        )
        events.append(
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"value":"wire"}'},
            }
        )
        events.append({"type": "content_block_stop", "index": 2})
    events.append(
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": "tool_use" if tool else "end_turn",
                "stop_sequence": None,
            },
            "usage": {"output_tokens": 5},
        }
    )
    events.append({"type": "message_stop"})
    content = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return HTTPResponse(content.encode())


def responses_completion(*, tool=False):
    events = []
    base = {
        "id": "resp-1" if tool else "resp-2",
        "object": "response",
        "created_at": 1,
        "model": "wire-model",
        "status": "in_progress",
        "output": [],
        "parallel_tool_calls": True,
    }
    events.append({"type": "response.created", "response": base})
    if tool:
        item = {
            "id": "fc-1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "echo",
            "arguments": "",
            "status": "in_progress",
        }
        events.append({"type": "response.output_item.added", "output_index": 0, "item": item})
        events.append(
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "fc-1",
                "delta": '{"value":"wire"}',
            }
        )
        item = {**item, "arguments": '{"value":"wire"}', "status": "completed"}
        events.append(
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "fc-1",
                "arguments": item["arguments"],
            }
        )
    else:
        item = {
            "id": "msg-1",
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
        events.append({"type": "response.output_item.added", "output_index": 0, "item": item})
        events.append(
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "item_id": "msg-1",
                "delta": "done",
            }
        )
        item = {
            **item,
            "status": "completed",
            "content": [{"type": "output_text", "text": "done", "annotations": []}],
        }
    events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
    usage = {
        "input_tokens": 20,
        "output_tokens": 5,
        "total_tokens": 25,
        "input_tokens_details": {"cached_tokens": 12},
        "output_tokens_details": {"reasoning_tokens": 3},
    }
    events.append(
        {
            "type": "response.completed",
            "response": {**base, "status": "completed", "output": [item], "usage": usage},
        }
    )
    content = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return HTTPResponse(content.encode())
