"""话题材料准备状态（照 ZCode topicPreparationState.ts 抄）。

仅保留未提交材料；对外投影不包含文件二进制，重试仍由入站授权链处理。
"""


class TopicPreparationState:
    def __init__(self):
        self._waiting_stop: set[str] = set()
        self._topics: dict[str, dict[str, dict]] = {}

    def add(self, key: str, message: dict) -> None:
        actor = message.get("actor", {}) or {}
        msg_id = actor.get("providerMessageId")
        if not msg_id:
            raise ValueError("Topic message identity is missing")
        entries = self._topics.setdefault(key, {})
        entries[msg_id] = {
            "message": message,
            "status": "waitingStop" if key in self._waiting_stop else "preparing",
        }

    def set_waiting_stop(self, key: str, waiting: bool) -> None:
        # 停止确认可能先于材料授权完成；保存话题阶段，让稍后加入的消息继承等待状态。
        if waiting:
            self._waiting_stop.add(key)
        else:
            self._waiting_stop.discard(key)
        for entry in self._topics.get(key, {}).values():
            if entry["status"] != "failed":
                entry["status"] = "waitingStop" if waiting else "preparing"

    def update(self, key: str, ids: list[str], status: str, error: str | None = None) -> None:
        for msg_id in ids:
            entry = self._topics.get(key, {}).get(msg_id)
            if entry:
                entry["status"] = status
                entry["error"] = error

    def snapshot(self, key: str) -> list[dict]:
        result = []
        for msg_id, entry in self._topics.get(key, {}).items():
            actor = entry["message"].get("actor", {}) or {}
            snapshot = {
                "messageId": msg_id,
                "senderName": actor.get("displayName") or actor.get("providerUserId"),
                "text": entry["message"].get("text", ""),
                "contentParts": entry["message"].get("contentParts", []),
                "status": entry["status"],
            }
            if entry.get("error"):
                snapshot["error"] = entry["error"]
            result.append(snapshot)
        return result

    def retry(self, key: str, msg_id: str) -> dict | None:
        entry = self._topics.get(key, {}).get(msg_id)
        return entry["message"] if entry and entry["status"] == "failed" else None

    def remove(self, key: str, ids: list[str]) -> None:
        entries = self._topics.get(key)
        if not entries:
            return
        for msg_id in ids:
            entries.pop(msg_id, None)
        if not entries:
            self._topics.pop(key, None)

    def clear(self, key: str | None = None) -> None:
        if key:
            self._topics.pop(key, None)
            self._waiting_stop.discard(key)
        else:
            self._topics.clear()
            self._waiting_stop.clear()
