"""提前压缩和直接压缩，在 1M 上下文、真实模型上各走一遍，量压缩让用户多等了多久。

    python evals/compaction_timing.py            # 两种都跑
    python evals/compaction_timing.py --mode direct

两种方式用同一个 CompactionMiddleware，只差开始生成摘要的时机：
- early（pycode 的做法）：80% 时在后台开始生成摘要，模型照常工作；到 90% 时换上摘要；
- direct：start_at 设成和 adopt_at 一样，到 90% 才开始生成摘要，这一步等摘要生成完再调用模型。

模型不开 thinking。对话是造出来的：每一轮是用户让模型读一个文件、模型调用 read、文件内容、
模型的一句说明。文件取自 .venv 里的 Python 源码，按路径排序，每次一样。
先用一次小请求量出真实 tokens 与 pycode 估算值的比例，造出的对话按真实 tokens 定大小；
最后一条模型回复带上这个真实大小，和真实会话一样由它判断上下文有多满。

early 从 80% 出发：每一步先往对话里加约 25k tokens 的已完成的一轮（相当于模型读了一个文件），
再发一句“继续”，这是一次真实的满上下文请求；直到换上摘要。direct 直接从 90% 出发，只有一步。
加内容本身不花时间，真实使用中还有工具运行的时间，所以 early 的后台摘要实际上只会更从容。

结果写到 evals/results/compaction-timing-<时间>.json（被 .gitignore 忽略）并打印。
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["PYCODE_HOME"] = tempfile.mkdtemp(prefix="pycode-timing-")  # 不碰用户的会话和记忆

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

import pycode.middleware.compaction as compaction_module  # noqa: E402
from pycode.agent import open_agent  # noqa: E402
from pycode.app.stream import Notice, Usage, events  # noqa: E402
from pycode.compaction.budget import estimate_message, estimate_text  # noqa: E402
from pycode.compaction.summarize import serialize  # noqa: E402
from pycode.config import Settings  # noqa: E402
from pycode.context import Context  # noqa: E402
from pycode.models import create_model, load_providers  # noqa: E402
from pycode.tools.files import Files  # noqa: E402

OVERHEAD = 3_000  # 系统提示词加工具定义，大约
STEP_TOKENS = 25_000  # early 每一步加进对话的真实 tokens
FILE_CHARS = 40_000  # 一次 read 最多带回的字符（read 工具上限 50KB）
CONTINUE = "继续。这是测试，只回复 OK。"


def corpus():
    """(相对路径, 内容)：.venv 里的 Python 源码，按路径排序，大于 4KB 的。"""
    site = next((ROOT / ".venv" / "lib").glob("python*/site-packages"))
    for path in sorted(site.rglob("*.py")):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > 4_000:
            yield str(path.relative_to(site)), text[:FILE_CHARS]


def one_turn(path: str, text: str) -> list:
    """用户让模型读一个文件，模型读完说一句：一轮完整的对话。"""
    call_id = f"call_{uuid.uuid4().hex[:12]}"
    first_line = next((line for line in text.splitlines() if line.strip()), "")[:200]
    return [
        HumanMessage(f"读一下 {path}，说说它做什么。", id=str(uuid.uuid4())),
        AIMessage(
            "",
            tool_calls=[{"name": "read", "args": {"path": path}, "id": call_id}],
            additional_kwargs={"reasoning_content": "先读这个文件。"},
            id=str(uuid.uuid4()),
        ),
        ToolMessage(text, tool_call_id=call_id, name="read", id=str(uuid.uuid4())),
        AIMessage(
            f"{path} 是一个 Python 模块，开头是：{first_line}。它定义了若干函数和类。",
            additional_kwargs={"reasoning_content": "概括一下。"},
            id=str(uuid.uuid4()),
        ),
    ]


class History:
    """按真实 tokens 造对话，文件不重复。"""

    def __init__(self, ratio: float):
        self.ratio = ratio  # 真实 tokens / pycode 的估算值
        self.files = corpus()

    def turns(self, real_tokens: int) -> list:
        messages, estimate = [], 0
        while estimate * self.ratio < real_tokens:
            turn = one_turn(*next(self.files))
            messages += turn
            estimate += sum(estimate_message(m) for m in turn)
        return messages

    def seed(self, real_tokens: int) -> list:
        """一段对话，最后一条模型回复报告的 input_tokens 就是它的真实大小。"""
        messages = self.turns(real_tokens - OVERHEAD)
        before = OVERHEAD + sum(estimate_message(m) for m in messages[:-1])
        usage = {"input_tokens": round(before * self.ratio), "output_tokens": 30}
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        messages[-1] = messages[-1].model_copy(update={"usage_metadata": usage})
        return messages


async def calibrate(provider) -> float:
    """真实 tokens 与估算值之比，用一段约 4 万估算 tokens 的同类内容量一次。"""
    model = create_model(provider, ROOT)
    files = corpus()
    sample = []
    while sum(estimate_message(m) for m in sample) < 40_000:
        sample += one_turn(*next(files))
    text = serialize(sample) + "\n\n这是测试，只回复 OK。"
    reply = await model.ainvoke([HumanMessage(text)])
    return reply.usage_metadata["input_tokens"] / (estimate_text(text) + 4)


class Clock:
    """记下摘要什么时候开始、什么时候生成完，换上摘要时等了多久。"""

    def __init__(self, compaction):
        self.t0 = time.monotonic()
        self.summary_started = self.summary_done = None
        self.summary_input_tokens = None  # 摘要请求的估算大小（工具结果每条截到 2000 字符）
        self.adopt_waits = []
        original_summarize = compaction_module.summarize
        original_adopt = compaction.adopt

        async def summarize(model, messages, cut, ongoing, max_tokens):
            self.summary_started = self.now()
            self.summary_input_tokens = estimate_text(serialize(messages[:cut]))
            try:
                return await original_summarize(model, messages, cut, ongoing, max_tokens)
            finally:
                self.summary_done = self.now()

        async def adopt(messages, writer, limit=None):
            started = self.now()
            try:
                return await original_adopt(messages, writer, limit)
            finally:
                self.adopt_waits.append(round(self.now() - started, 2))

        self.restore = lambda: setattr(compaction_module, "summarize", original_summarize)
        compaction_module.summarize = summarize
        compaction.adopt = adopt

    def now(self) -> float:
        return time.monotonic() - self.t0


async def run_turn(opened, config, workspace, text: str, clock: Clock) -> dict:
    started = clock.now()
    notices, usage = [], None
    context = Context(files=Files(workspace))
    async for event in events(opened.agent, {"messages": [HumanMessage(text)]}, config, context):
        if isinstance(event, Notice):
            notices.append(event.text)
        elif isinstance(event, Usage):
            usage = event
    return {
        "started": round(started, 2),
        "seconds": round(clock.now() - started, 2),
        "input_tokens": usage and usage.input_tokens,
        "cached_tokens": usage and usage.cached_tokens,
        "notices": notices,
    }


async def run_mode(mode: str, settings: Settings, ratio: float) -> dict:
    history = History(ratio)
    async with open_agent(settings) as opened:
        opened.memory.consolidate_later = lambda messages: None  # 不让记忆整理混进计时
        compaction = opened.compaction
        if mode == "direct":
            compaction.start_at = compaction.adopt_at
        start_at, adopt_at = compaction.start_at, compaction.adopt_at
        config = {"configurable": {"thread_id": f"timing-{mode}"}}
        first = adopt_at + 5_000 if mode == "direct" else start_at + 5_000
        await opened.agent.aupdate_state(config, {"messages": history.seed(first)})
        clock = Clock(compaction)
        try:
            steps = [await run_turn(opened, config, settings.workspace, CONTINUE, clock)]
            while not any("压缩成摘要" in n for n in steps[-1]["notices"]) and len(steps) < 10:
                turns = history.turns(STEP_TOKENS)
                await opened.agent.aupdate_state(config, {"messages": turns})
                steps.append(await run_turn(opened, config, settings.workspace, CONTINUE, clock))
        finally:
            clock.restore()
        adopted = next(s for s in steps if any("压缩成摘要" in n for n in s["notices"]))
        return {
            "mode": mode,
            "start_at": start_at,
            "adopt_at": adopt_at,
            "seed_real_tokens": first,
            "summary_input_estimate": clock.summary_input_tokens,
            "summary_seconds": round(clock.summary_done - clock.summary_started, 2),
            "summary_started": round(clock.summary_started, 2),
            "summary_done": round(clock.summary_done, 2),
            "adopt_waits": clock.adopt_waits,
            "compaction_turn_seconds": adopted["seconds"],
            "context_after_compaction": adopted["input_tokens"],
            "steps": steps,
        }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["early", "direct", "both"], default="both")
    args = parser.parse_args()
    provider = load_providers(ROOT / "providers.json")[0]
    # 满上下文的请求比默认 60 秒慢，放宽超时，免得重试混进计时。
    # 不开 thinking：开着时满上下文请求要等模型读完、想完才返回，测不出压缩本身的差别。
    provider = replace(provider, timeout_seconds=900, thinking={})
    providers_file = Path(os.environ["PYCODE_HOME"]) / "providers.json"
    providers_file.write_text(json.dumps([asdict(provider)]))
    settings = Settings(workspace=ROOT, providers_file=providers_file, sandbox=False)
    ratio = await calibrate(provider)
    print(f"真实/估算 tokens = {ratio:.3f}", flush=True)
    modes = ["early", "direct"] if args.mode == "both" else [args.mode]
    results = {"model": provider.model_id, "context_window": provider.context_window}
    results |= {"ratio": round(ratio, 4), "runs": []}
    for mode in modes:
        print(f"\n== {mode}", flush=True)
        result = await run_mode(mode, settings, ratio)
        results["runs"].append(result)
        print(json.dumps(result, ensure_ascii=False, indent=1), flush=True)
    out = ROOT / "evals" / "results" / f"compaction-timing-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\n写入 {out}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
