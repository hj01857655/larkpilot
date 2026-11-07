"""配置/状态持久化仓库（照 ZCode repo.ts 抄）。

- 全部写入走 atomic write（tmp + os.replace），崩溃不产生半份配置
- read/write/update 三段式：update 用闭包在最新快照上改，写回后返回新值
"""

import json
import os
import tempfile
from typing import Callable


def atomic_write(path: str, payload: dict) -> None:
    """tmp 文件 + os.replace，保证原子性。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


class BotRepo:
    """bot-config / bot-state 的单文件仓库。

    file 布局约定（对齐 ZCode 文件名）：
    - bot-config.v3.json   {version: 3, bots: [...]}
    - bot-state.v3.json    {version: 3, bots: {contextKey: {...}}}
    """

    def __init__(self, config_path: str, state_path: str):
        self._config_path = config_path
        self._state_path = state_path

    # ── config ────────────────────────────────────────────

    async def read_config(self) -> dict:
        return _read_json(self._config_path, default={"version": 3, "bots": []})

    async def write_config(self, config: dict) -> None:
        atomic_write(self._config_path, config)

    async def update_config(self, mutate: Callable[[dict], dict]) -> dict:
        current = await self.read_config()
        updated = mutate(current)
        await self.write_config(updated)
        return updated

    # ── state ─────────────────────────────────────────────

    async def read_state(self) -> dict:
        return _read_json(self._state_path, default={"version": 3, "bots": {}})

    async def write_state(self, state: dict) -> None:
        atomic_write(self._state_path, state)

    async def update_context(self, context_key: str, mutate: Callable[[dict | None], dict]) -> dict:
        state = await self.read_state()
        bots = state.setdefault("bots", {})
        current = bots.get(context_key)
        bots[context_key] = mutate(current)
        await self.write_state(state)
        return bots[context_key]


def _read_json(path: str, default: dict) -> dict:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # 损坏文件回默认值，由调用方决定是否重建
        return default
