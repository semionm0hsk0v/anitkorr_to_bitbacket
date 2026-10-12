"""Параллельные операции с упорядоченным результатом и завершением дочерних задач."""

import asyncio
from collections.abc import Coroutine
from typing import Any


async def gather_results[T](*operations: Coroutine[Any, Any, T]) -> list[T]:
    tasks = [asyncio.create_task(operation) for operation in operations]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
