"""群消息发送限流（照 ZCode groupDeliveryThrottle.ts 抄）。

群消息共享发送额度：同一 chatKey 两次发送间隔至少 250ms。
只预留/管理下次可发送的时间，不保存也不执行任务输入。
"""

import asyncio
import time
from typing import Dict

SEND_INTERVAL_MS = 250


class GroupDeliveryThrottle:
    def __init__(self):
        self._next_send_at: Dict[str, float] = {}

    async def acquire(self, chat_key: str) -> None:
        """预约当前时刻之后的发送额度；如果已有未到的预约则等待剩余时间。"""
        now = time.monotonic() * 1000

        at = max(now, self._next_send_at.get(chat_key, now))
        self._next_send_at[chat_key] = at + SEND_INTERVAL_MS

        wait_ms = at - now
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)

        # 空闲 key 不常驻；但预约刚发出时不能清掉它的额度。
        # 仅当预约在"当前时刻"之前（已经过期）才清除——和 ZCode 原文
        # `(nextSendAt.get(chatKey) ?? 0) <= Date.now()` 语义一致。
        if self._next_send_at.get(chat_key, 0) <= time.monotonic() * 1000:
            self._next_send_at.pop(chat_key, None)
