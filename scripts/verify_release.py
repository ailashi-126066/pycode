"""Release checks: lint, architecture, build, package contents and a fresh wheel installation."""

import argparse
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def verify_distribution(directory):
    """Check built wheel/sdist for package identity, required notices and leaked caches."""
    wheel = next(directory.glob("pycode-*.whl"))
    sdist = next(directory.glob("pycode-*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = archive.namelist()
        metadata = archive.read("pycode-0.1.0.dist-info/METADATA").decode()
        entrypoints = archive.read("pycode-0.1.0.dist-info/entry_points.txt").decode()
    with tarfile.open(sdist) as archive:
        sdist_files = archive.getnames()
    forbidden = (
        "/.venv/",
        "/__pycache__/",
        "/.runtime-tests/",
        "/.fix-20261013/",
        "/audit-20261013/",
        "/evals/cache/",
        "/evals/work/",
        "/evals/results/",
        "/.worktrees/",
        "/.env",
        "/.git/",
    )
    for name in [*wheel_files, *sdist_files]:
        for fragment in forbidden:
            if fragment in "/" + name and not name.endswith("/.env.example"):
                raise ValueError("Distribution includes private/generated content: " + name)
    # The wheel is the installable product: only the pycode package and its metadata.
    for name in wheel_files:
        if not name.startswith(("pycode/", "pycode-0.1.0.dist-info/")):
            raise ValueError("Wheel includes non-product content: " + name)
    assert "pycode = pycode.app.cli.main:main" in entrypoints and "\npy =" not in entrypoints
    for package, version in (
        ("deepagents", "0.7.32"),
        ("langchain", "1.4.7"),
        ("langgraph", "1.2.18"),
    ):
        assert package in metadata and "==" + version in metadata
    assert "langgraph-checkpoint-sqlite" in metadata
    assert "pycode/licenses/pi-MIT.txt" in wheel_files
    assert "pycode/licenses/opencode-MIT.txt" in wheel_files
    assert "pycode/licenses/exercism-MIT.txt" in wheel_files
    print(
        json.dumps(
            {
                "wheel": wheel.name,
                "sdist": sdist.name,
                "wheel_files": len(wheel_files),
                "sdist_files": len(sdist_files),
                "status": "passed",
            }
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        help="new directory for logs, report and dist (default: tests/.runtime-tests/release-*)",
    )
    parser.add_argument("--with-evals", action="store_true")
    parser.add_argument("--check-distribution", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check_distribution:
        verify_distribution(args.check_distribution)
        return 0
    root = ROOT
    default = Path("tests/.runtime-tests") / ("release-" + datetime.now().strftime("%Y%m%dT%H%M%S"))
    evidence = (root / (args.output or default)).resolve()
    if not evidence.is_relative_to(root):
        raise RuntimeError("Release artifacts must remain inside the project")
    if evidence.exists():
        raise RuntimeError("Use a new output directory; preserve prior runs")
    evidence.mkdir(parents=True)
    distribution = evidence / "dist"
    report_path = evidence / "release-checks.json"
    has_evals = (root / "evals" / "__main__.py").is_file()
    report = {
        "extras": ["eval"] if args.with_evals else [],
        "status": "running",
        "checks": [],
    }

    def check(name, argv, *, cwd=root, environment=None):
        started = time.monotonic()
        result = subprocess.run(
            argv, cwd=cwd, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        log = evidence / (name + ".log")
        log.write_bytes(result.stdout)
        report["checks"].append(
            {
                "name": name,
                "argv": [str(item) for item in argv],
                "cwd": str(cwd),
                "exit_code": result.returncode,
                "elapsed_seconds": time.monotonic() - started,
                "log": log.relative_to(root).as_posix(),
            }
        )
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(name + ": " + str(result.returncode), flush=True)
        if result.returncode:
            report["status"] = "failed"
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            raise RuntimeError("Release check failed: " + name)
        return result.stdout

    python = sys.executable
    script = Path(__file__).resolve()
    sources = ["src", "tests", "scripts", *(["evals"] if has_evals else [])]
    check("lint", [python, "-m", "ruff", "check", "--no-cache", *sources])
    check("format", [python, "-m", "ruff", "format", "--no-cache", "--check", *sources])
    check("architecture", [python, "scripts/check_architecture.py"])
    check("pip-check", [python, "-m", "pip", "check"])
    check("build", [python, "-m", "build", "--no-isolation", "--outdir", str(distribution)])
    check("distribution", [python, str(script), "--check-distribution", str(distribution)])
    install_root = (root / "tests/.runtime-tests" / ("install-" + uuid4().hex[:8])).resolve()
    if not install_root.is_relative_to((root / "tests").resolve()) or install_root.exists():
        raise RuntimeError("Use a new test-owned clean installation directory")
    check("venv", [python, "-m", "venv", str(install_root)])
    installed_python = install_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = install_root / ("Scripts/pycode.exe" if os.name == "nt" else "bin/pycode")
    constraints = evidence / "constraints.txt"
    constraints.write_text(
        (root / "requirements-dev.lock").read_text().replace("langchain[mcp]", "langchain")
    )
    wheel = distribution / "pycode-0.1.0-py3-none-any.whl"
    check(
        "install",
        [
            str(installed_python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--only-binary=:all:",
            "-c",
            str(constraints),
            str(wheel) + ("[eval]" if args.with_evals else ""),
        ],
    )
    clean_environment = dict(os.environ)
    clean_environment.pop("PYTHONPATH", None)
    clean_environment.pop("PYTHONHOME", None)
    check(
        "version", [str(executable), "--version"], cwd=install_root, environment=clean_environment
    )
    doctor = parse_json_output(
        check(
            "doctor",
            [str(executable), "--data-dir", str(install_root / "data"), "doctor", "--json"],
            cwd=install_root,
            environment=clean_environment,
        )
    )
    assert doctor["workspace"]["path"] == str(install_root)
    check(
        "installed-pip-check",
        [str(installed_python), "-m", "pip", "check"],
        cwd=install_root,
        environment=clean_environment,
    )
    check("help", [str(executable), "--help"], cwd=install_root, environment=clean_environment)
    check(
        "module-help",
        [str(installed_python), "-m", "pycode", "--help"],
        cwd=install_root,
        environment=clean_environment,
    )
    probe = """import json, pathlib, sys
from importlib.metadata import requires, version
import pycode
assert pathlib.Path(pycode.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
direct = requires('pycode')
for requirement in direct:
    name = requirement.split(';')[0].split('[')[0].split('==')[0].split('>=')[0].strip()
    assert name not in {'mcp', 'langchain-mcp-adapters', 'langgraph-checkpoint-sqlite', 'aiosqlite'}
for name, expected in {'deepagents': '0.7.32', 'langchain': '1.4.7', 'langgraph': '1.2.18'}.items():
    assert version(name) == expected
print(json.dumps({'module': pycode.__file__, 'direct_dependencies': direct, 'status': 'passed'}))
"""
    check(
        "installed-dependencies",
        [str(installed_python), "-c", probe],
        cwd=install_root,
        environment=clean_environment,
    )
    verify_minimal_run(check, executable, install_root, clean_environment)
    if args.with_evals:
        eval_probe = """import json
from importlib.metadata import version
from langsmith import tracing_context
from openevals.llm import create_async_llm_as_judge
from agentevals.trajectory.llm import create_async_trajectory_llm_as_judge
from agentevals.trajectory.match import create_trajectory_match_evaluator
expected = {'openevals': '0.2.0', 'agentevals': '0.0.9', 'langfuse': '4.17.0'}
for name, pinned in expected.items():
    assert version(name) == pinned
assert callable(create_async_llm_as_judge)
assert callable(create_async_trajectory_llm_as_judge)
judge = create_trajectory_match_evaluator(trajectory_match_mode='strict')
with tracing_context(enabled=False):
    assert judge(outputs=[], reference_outputs=[])['score'] is True
    assert judge(outputs=[{'role': 'assistant', 'content': 'unexpected'}],
                 reference_outputs=[])['score'] is False
print(json.dumps({'status': 'passed', 'versions': expected}))
"""
        check(
            "installed-eval-dependencies",
            [str(installed_python), "-c", eval_probe],
            cwd=install_root,
            environment=clean_environment,
        )
        # evals/ ships only in the sdist; run its entry point from the checkout against the
        # freshly installed pycode (no PYTHONPATH, so src/ is not on the path).
        if has_evals:
            check(
                "evals-help",
                [str(installed_python), "-m", "evals", "coding", "--help"],
                cwd=root,
                environment=clean_environment,
            )
        else:
            print("evals-help: skipped (no evals/ package in this checkout)", flush=True)
    report["status"] = "passed"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


def verify_minimal_run(check, executable, install_root, environment):
    """Exercise the installed CLI/model/history loop against a local protocol server."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            base = {"id": "delivery", "object": "chat.completion.chunk", "model": "wire-model"}
            chunks = [
                {
                    **base,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"}}],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {
                    **base,
                    "choices": [],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 1, "total_tokens": 21},
                },
            ]
            body = (
                "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                + "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    providers = install_root / "providers.json"
    providers.write_text(
        json.dumps(
            [
                {
                    "name": "delivery",
                    "implementation": "openai",
                    "model_id": "wire-model",
                    "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "api_key_env": "PYCODE_DELIVERY_TEST_KEY",
                    "context_window": 32000,
                    "max_output_tokens": 4096,
                    "openai_api": "chat_completions",
                    "supports_tools": True,
                }
            ]
        )
    )
    try:
        result = parse_json_output(
            check(
                "minimal-run",
                [
                    str(executable),
                    "--data-dir",
                    str(install_root / "data"),
                    "--memory-mode",
                    "off",
                    "--providers",
                    str(providers),
                    "--cached",
                    "--output-format",
                    "json",
                    "-p",
                    "Reply OK without using tools.",
                ],
                cwd=install_root,
                environment={**environment, "PYCODE_DELIVERY_TEST_KEY": "local-protocol-only"},
            )
        )
        assert result["outcome"] == "completed" and result["final_text"] == "OK"
        assert len(requests) == 1 and requests[0]["model"] == "wire-model"
        assert result["usage"]["session"]["request_count"] == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def parse_json_output(output):
    # Keep SDK diagnostics in the saved combined log; the CLI result is its last line.
    return json.loads(output.decode("utf-8").splitlines()[-1])


if __name__ == "__main__":
    raise SystemExit(main())
