"""群队列卡片（照 ZCode groupQueueCard.ts 抄）。

- rank 单向：waiting(0) < working(1) < done(2)，迟到的 admission 不能覆盖终态
- 同一 key 的变更经 operations 链串行化，前一个完成才跑下一个
- 变更落盘后读取最新状态：有卡片且 cardStatus != status 才 render，然后写回 cardStatus
"""

from typing import Awaitable, Callable

import asyncio

# 状态单向等级
RANK = {"waiting": 0, "working": 1, "done": 2}


def _rank(state: str) -> int:
    return RANK.get(state, 0)


class GroupQueueCard:
    def __init__(
        self,
        read: Callable[[str], Awaitable[dict | None]],
        mutate: Callable[[str, Callable[[dict], dict]], Awaitable[dict | None]],
        render: Callable[[str, str, str], Awaitable[None]],
    ):
        self._read = read
        self._mutate = mutate
        self._render = render
        self._operations: dict[str, asyncio.Task] = {}

    async def _change(self, key: str, mutate_fn: Callable[[dict], dict]) -> None:
        # 串行化：等前一个同 key 操作结束（失败也算结束，不阻断后续）
        prev = self._operations.get(key)
        if prev is not None:
            try:
                await prev
            except Exception:
                pass
        task = asyncio.ensure_future(self._run_change(key, mutate_fn))
        self._operations[key] = task
        # 自清理：只有链头还是自己时才摘除
        task.add_done_callback(
            lambda _t: self._operations.pop(key, None) if self._operations.get(key) is task else None
        )
        await task

    async def _run_change(self, key: str, mutate_fn: Callable[[dict], dict]) -> None:
        await self._mutate(key, mutate_fn)
        current = await self._read(key)
        if not current or not current.get("cardMessageId"):
            return
        status = current.get("status")  # attach 后可能尚无 status，对齐 TS undefined
        if current.get("cardStatus") == status:
            return
        await self._render(key, current["cardMessageId"], status)
        # 用最新状态写回，避免覆盖并发变更
        await self._mutate(key, lambda latest: {**latest, "cardStatus": status})

    async def update(self, key: str, status: str) -> None:
        """rank 单向合并（mutate_fn 同步，对齐 ZCode 原文箭头函数）。"""
        def _mutate(current: dict) -> dict:
            merged = status if _rank(status) > _rank(current.get("status", "waiting")) else current.get("status", "waiting")
            return {**current, "status": merged}
        await self._change(key, _mutate)

    async def attach(self, key: str, card_message_id: str) -> None:
        """挂载卡片：重置 cardStatus 为 waiting（卡片新渲染总是从等待态开始）。"""
        def _mutate(current: dict) -> dict:
            return {**current, "cardMessageId": card_message_id, "cardStatus": "waiting"}
        await self._change(key, _mutate)
