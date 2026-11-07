"""群成员目录缓存（照 ZCode groupMemberDirectory.ts 抄）。

- 成功缓存 60s；失败短缓存 30s，避免每条群消息重复查询
- 缓存的是异步任务本身：并发请求共享同一次分页抓取
- LRU 128 上限
- Python SDK 无群成员列表封装，readPage 由调用方 HTTP 直调
  GET /im/v1/chats/:chat_id/members?page_size=..&page_token=.. 注入
"""

import asyncio
import time
from typing import Awaitable, Callable

SUCCESS_TTL_MS = 60_000
FAILURE_TTL_MS = 30_000
MAX_CACHE = 128


class GroupMemberDirectory:
    def __init__(self):
        self._cache: dict[str, dict] = {}  # key -> {expires, task}

    async def get(
        self,
        key: str,
        read_page: Callable[[str | None], Awaitable[dict]],
    ) -> dict[str, str]:
        """返回 {open_id: name}。

        read_page(cursor) -> {items: [{member_id, name}], has_more, page_token}
        """
        existing = self._cache.get(key)
        now = time.monotonic() * 1000
        if existing and existing["expires"] > now:
            return await existing["task"]

        task = asyncio.ensure_future(self._fetch(key, read_page))
        self._cache.pop(key, None)  # LRU：挪到最新
        self._cache[key] = {"expires": float("inf"), "task": task}

        try:
            result = await task
        except Exception:
            # 缺权限时短缓存失败窗口，不暴露不完整的分页结果
            entry = self._cache.get(key)
            if entry is not None and entry["task"] is task:
                entry["expires"] = time.monotonic() * 1000 + FAILURE_TTL_MS
            raise
        entry = self._cache.get(key)
        if entry is not None and entry["task"] is task:
            entry["expires"] = time.monotonic() * 1000 + SUCCESS_TTL_MS
        if len(self._cache) > MAX_CACHE:
            oldest = next(iter(self._cache))
            self._cache.pop(oldest, None)
        return result

    async def _fetch(
        self,
        key: str,
        read_page: Callable[[str | None], Awaitable[dict]],
    ) -> dict[str, str]:
        names: dict[str, str] = {}
        visited: set[str] = set()
        cursor: str | None = None
        while True:
            page = await read_page(cursor)
            for member in page.get("items") or []:
                member_id = member.get("member_id") or ""
                name = member.get("name") or ""
                # 只收 ou_ 前缀的真实 open_id 与非空名字
                if member_id.startswith("ou_") and name.strip():
                    names[member_id] = name.strip()
            if not page.get("has_more"):
                break
            cursor = page.get("page_token")
            if not cursor or cursor in visited:
                raise ValueError("Invalid group member pagination")
            visited.add(cursor)
        return names

    def clear(self) -> None:
        self._cache.clear()
