"""
按钮回调幂等表。

照 ZCode channelReply.ts 投递幂等语义抄：飞书卡片回调 HTTP 在
服务端超时/非 2xx 时会重试——用户点一次"是",如果 callback 服务端卡了
3 秒才响应,飞书重推同一回调,Claude 会把同一个答案再跑一遍。

稳定三元组 (open_id, open_message_id, value_digest):
  - open_id        谁点的
  - open_message_id   哪张卡(同一张卡片同一个按钮只会触发一条)
  - value_digest   value 字段(含 reply/cmd/sid/mode 等)的 sha256

靠 seen_store.SeenStore 的落盘+TTL+LRU 语义:callback 重试通常在
数十秒内到达,TTL=120s 足够盖住重试窗口又可以让同一用户在换张卡
后再次合法点击。
"""

import hashlib
import json
import os
from typing import Optional

from seen_store import SeenStore

DEFAULT_PATH = os.path.expanduser("~/.feishu-claude/callback_seen.json")


def _digest_value(value: object) -> str:
    """把按钮 value 字段序列化为稳定指纹;不可排序的部分用 default=str 兜底。"""
    payload = json.dumps(
        value if value is not None else {},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_key(open_id: str, open_message_id: str, value: object,
             action_type: Optional[str] = None) -> str:
    """构造稳定去重 key。

    open_message_id 不为空时,加进 key;卡片回调里它是识别"哪张卡"的
    唯一标识,空值时退化到 (open_id, value_digest),兼容老调用方。
    action_type 已含在 value_digest 中,这里再拼接一次以分隔 namespace,
    避免跨 action_type 的 key 穿越。
    """
    digest = _digest_value(value)
    parts = [str(open_id).replace(":", ""), ""]
    if action_type:
        parts.append(action_type.replace(":", ""))
    if open_message_id:
        parts.append(str(open_message_id).replace(":", ""))
    parts.append(digest)
    return ":".join(parts)


class CallbackStore:
    """包装 SeenStore 提供 caption 语义:重复命中返回 True。"""

    def __init__(self, path: str = "", ttl: int = 120, max_items: int = 5000):
        self._store = SeenStore(
            path=path or DEFAULT_PATH,
            ttl=ttl,
            max_items=max_items,
        )

    def already_seen(self, open_id: str, open_message_id: str, value: object,
                     action_type: Optional[str] = None) -> bool:
        """返回 True = 重复(早退);False = 首次(并登记,后续重放会被挡)。"""
        if not open_id:
            return False
        key = make_key(open_id, open_message_id, value, action_type)
        return self._store.seen(key)

    def clear(self) -> None:
        self._store.clear()

    def size(self) -> int:
        return self._store.size()
