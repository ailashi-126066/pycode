"""`python -m evals`: evaluation entry points kept outside the pycode package."""

import argparse
import asyncio
import io
import sys
from dataclasses import replace
from pathlib import Path

from evals.coding.assembly import build_evaluation_runner
from pycode.app.bootstrap import compose_application
from pycode.app.configuration.providers import load_providers
from pycode.app.configuration.settings import load_config
from pycode.kernel import DomainError, json_dumps

QUALITY_SCRIPTS = """quality evaluations (run from the repository root; each accepts --help):
  python evals/scripts/run_output_quality.py
  python evals/scripts/run_trajectory_quality.py
  python evals/scripts/summarize_quality_suite.py
  python evals/scripts/verify_official_eval_interfaces.py
coding corpus helpers:
  python evals/scripts/fetch_eval_corpus.py
  python evals/scripts/freeze_eval_manifest.py
  python evals/scripts/verify_eval_baselines.py --work DIR --output FILE
  python evals/scripts/compare_eval_runs.py"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="pycode evaluations",
        epilog=QUALITY_SCRIPTS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workspace", help="Project directory")
    parser.add_argument("--data-dir", help="User configuration and storage directory")
    parser.add_argument("--memory-mode", choices=("auto", "off"))
    parser.add_argument("--output-format", choices=("text", "json", "stream-json"), default="text")
    commands = parser.add_subparsers(dest="command")
    coding = commands.add_parser(
        "coding", help="Run fixed isolated Python coding evaluations serially"
    )
    coding.add_argument(
        "--providers", required=True, type=lambda value: Path(value).expanduser().resolve()
    )
    coding.add_argument("--manifest", default="evals/data/manifest.json")
    coding.add_argument("--cache", default="evals/cache/polyglot")
    coding.add_argument("--work", required=True)
    coding.add_argument("--results", required=True)
    coding.add_argument("--full", action="store_true")
    coding.add_argument("--keep-work", action="store_true")
    coding.add_argument("--langfuse", action="store_true")
    return parser


async def coding(args, config):
    output = compose_application(config).console(args.output_format)
    providers = await asyncio.to_thread(load_providers, args.providers)
    runner = build_evaluation_runner(
        config,
        providers,
        Path(args.manifest),
        Path(args.cache),
        Path(args.work),
        Path(args.results),
        langfuse=args.langfuse,
    )
    value = await runner.run(smoke=not args.full, cleanup=not args.keep_work)
    output.value({"model": providers[0].model_id, "results": value})
    return 0 if all(result["outcome"] == "passed" for result in value) else 1


def main(argv=None):
    if isinstance(sys.stdout, io.TextIOWrapper) and sys.stdout.encoding.lower() not in {
        "utf-8",
        "utf8",
    }:
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    config = replace(load_config(args.workspace, args.data_dir), memory_mode=args.memory_mode)
    try:
        return asyncio.run(coding(args, config))
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


if __name__ == "__main__":
    raise SystemExit(main())
