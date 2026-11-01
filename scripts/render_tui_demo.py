"""Capture deterministic actual Textual screens using a real SDK loopback fixture."""

import asyncio
import os
from pathlib import Path

from pycode.app import AppConfig, compose_application
from tests.support.execution import provider
from tests.support.http_provider import protocol_server
from tests.tools.adapters.test_questions import question_response


async def main(project):
    workspace = project / ".acceptance" / "tui-demo"
    output = project / ".acceptance" / "tui-demo-screens"
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    os.environ["PYCODE_RECOVERY_TEST_KEY"] = "deterministic-demo-key"
    questions = {
        "questions": [
            {
                "type": "text",
                "name": "language",
                "title": "Language",
                "message": "Which language should the project use?",
            },
            {
                "type": "radio",
                "name": "style",
                "title": "Style",
                "message": "Choose a style",
                "options": ["Compact", "Detailed"],
            },
        ]
    }
    with protocol_server([question_response(questions)]) as (url, _):
        app = compose_application(AppConfig(workspace, workspace / "data"), (provider(url),)).tui()
        async with app.run_test(size=(120, 45)) as pilot:
            await app.ready.wait()
            await app.send_text("Inspect the project, then ask about implementation preferences.")
            await app.wait_idle()
            await pilot.pause()
            for name in ("pycode-dark", "pycode-light"):
                app.theme = name
                await pilot.pause()
                svg = app.export_screenshot(title="pycode")
                await asyncio.to_thread(
                    (output / f"tui-{name}.svg").write_text, svg, encoding="utf-8"
                )


if __name__ == "__main__":
    asyncio.run(main(Path(__file__).resolve().parents[1]))
