"""
消息幂等表：把已处理的飞书 message_id 落盘，重启后不会重放。

状态文件 ~/.feishu-claude/seen_messages.json，格式 {"mid": <epoch_ts>, ...}；
加载时按 TTL 过滤过期项，进程内仍按 LRU 上限裁剪。

与 admission.py 共用同一套原子写（mkstemp + os.replace）。
"""

import json
import os
import tempfile
import threading
import time

STATE_FILE = os.path.expanduser("~/.feishu-claude/seen_messages.json")

# 单次写盘最少间隔；事件突发时聚合成一次 IO
_FLUSH_INTERVAL_SEC = 1.0


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _save(path: str, data: dict) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SeenStore:
    """内存 LRU + 落盘持久化；TTL 控制保留时长，MAX 控制条目上限。"""

    def __init__(self, path: str = "", ttl: int = 600, max_items: int = 2000):
        from collections import OrderedDict
        # 空字符串/None 回退默认路径：调用方常用 `expanduser(os.getenv(X,"")) or None` 传 None
        self.path = path or STATE_FILE
        self.ttl = ttl
        self.max_items = max_items
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, float]" = OrderedDict()
        self._dirty = False
        self._last_flush = 0.0
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        now = time.time()
        loaded = _load(self.path)
        with self._lock:
            for mid, ts in loaded.items():
                if now - ts <= self.ttl:
                    self._items[mid] = ts

    def seen(self, message_id: str) -> bool:
        """返回 True = 已处理过（重复）；False = 首次（并登记）。"""
        if not message_id:
            return False
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            if message_id in self._items:
                self._items.move_to_end(message_id)
                return True
            self._items[message_id] = now
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
            self._dirty = True
        self._maybe_flush(now)
        return False

    def _evict_expired_locked(self, now: float) -> None:
        for mid, ts in list(self._items.items()):
            if now - ts > self.ttl:
                del self._items[mid]
            else:
                break

    def _maybe_flush(self, now: float, force: bool = False) -> None:
        if not self._dirty:
            return
        if not force and now - self._last_flush < _FLUSH_INTERVAL_SEC:
            return
        with self._lock:
            if not self._dirty:
                return
            if not force and now - self._last_flush < _FLUSH_INTERVAL_SEC:
                return
            snapshot = dict(self._items)
            self._dirty = False
            self._last_flush = now
        try:
            _save(self.path, snapshot)
        except OSError as e:
            # 落盘失败只影响重启后的幂等；进程内内存表仍然有效，不能因此阻塞消息
            import sys
            print(f"[warn] seen store flush failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    def flush(self) -> None:
        """强制落盘；进程退出/定时持久化点调用。"""
        self._maybe_flush(time.time(), force=True)

    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._dirty = True
        self._maybe_flush(time.time(), force=True)
