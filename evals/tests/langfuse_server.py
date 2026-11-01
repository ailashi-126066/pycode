"""Real local HTTP endpoints for the public Langfuse SDK, including OTLP protobuf."""

import gzip
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


@contextmanager
def langfuse_server():
    requests = []
    spans = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            if self.path.endswith("/otel/v1/traces"):
                payload = ExportTraceServiceRequest()
                payload.ParseFromString(raw)
                for resource in payload.resource_spans:
                    for scope in resource.scope_spans:
                        spans.extend(scope.spans)
                requests.append({"path": self.path, "raw": raw})
                self.reply(b"", content_type="application/x-protobuf")
                return
            body = json.loads(raw)
            requests.append({"path": self.path, "body": body, "raw": raw})
            now = "2026-10-13T00:00:00Z"
            if self.path == "/api/public/v2/datasets":
                value = {
                    "id": "dataset",
                    "name": body["name"],
                    "metadata": body.get("metadata"),
                    "projectId": "project",
                    "createdAt": now,
                    "updatedAt": now,
                }
            elif self.path == "/api/public/dataset-items":
                value = {
                    "id": body["id"],
                    "status": "ACTIVE",
                    "input": body["input"],
                    "expectedOutput": body.get("expectedOutput"),
                    "metadata": body.get("metadata"),
                    "datasetId": "dataset",
                    "datasetName": body["datasetName"],
                    "createdAt": now,
                    "updatedAt": now,
                    "mediaReferences": [],
                }
            elif self.path == "/api/public/ingestion":
                value = {
                    "successes": [{"id": item["id"], "status": 201} for item in body["batch"]],
                    "errors": [],
                }
            else:
                self.reply(json.dumps({"message": "Unexpected SDK endpoint"}).encode(), status=404)
                return
            self.reply(json.dumps(value).encode())

        def reply(self, body, *, status=200, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), requests, spans
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=10)
        assert not worker.is_alive()
