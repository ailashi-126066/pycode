"""Pi's withFileMutationQueue: one file at a time, different files in parallel."""

import asyncio

from pycode.tools.builtin.paths import with_file_mutation_queue


async def test_same_file_mutations_run_one_after_another():
    order = []
    release = asyncio.Event()

    async def first():
        order.append("first-start")
        await release.wait()
        order.append("first-end")

    async def second():
        order.append("second-start")

    running = asyncio.gather(
        with_file_mutation_queue("/real/a.txt", first),
        with_file_mutation_queue("/real/a.txt", second),
    )
    await asyncio.sleep(0)
    assert order == ["first-start"]
    release.set()
    await running
    assert order == ["first-start", "first-end", "second-start"]


async def test_different_files_mutate_in_parallel():
    started = []
    release = asyncio.Event()

    async def hold(name):
        started.append(name)
        await release.wait()

    running = asyncio.gather(
        with_file_mutation_queue("/real/a.txt", lambda: hold("a")),
        with_file_mutation_queue("/real/b.txt", lambda: hold("b")),
    )
    await asyncio.sleep(0)
    assert sorted(started) == ["a", "b"]
    release.set()
    await running
