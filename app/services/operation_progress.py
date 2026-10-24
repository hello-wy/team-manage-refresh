"""Stream real operation milestones without buffering the HTTP response."""
import asyncio
import json
from contextlib import suppress
from typing import Awaitable, Callable

ProgressCallback = Callable[[str], Awaitable[None]]


async def report_progress(callback: ProgressCallback | None, stage: str) -> None:
    if callback is not None:
        await callback(stage)


async def stream_operation(operation: Callable[[ProgressCallback], Awaitable[dict]]):
    events: asyncio.Queue[dict | None] = asyncio.Queue()

    async def report(stage):
        await events.put({"type": "progress", "stage": stage})

    async def run():
        try:
            await events.put(await operation(report))
        finally:
            await events.put(None)

    task = asyncio.create_task(run())
    try:
        while (event := await events.get()) is not None:
            yield json.dumps(event, ensure_ascii=False) + "\n"
        await task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
