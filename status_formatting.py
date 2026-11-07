"""会话时间/工具进度文本格式化（照 ZCode statusFormatting.ts 抄）。"""

from datetime import datetime

MS_IN_SECOND = 1_000
MS_IN_MINUTE = 60 * MS_IN_SECOND
MS_IN_HOUR = 60 * MS_IN_MINUTE
MS_IN_DAY = 24 * MS_IN_HOUR


def task_status(task: dict) -> str:
    return task.get("status") or "running"


def format_task_running_duration(duration_ms: int) -> str:
    """把毫秒时长格式化为 "N d H m s" 紧凑形式；天/时/分只出现在 >0 时。"""
    safe = max(duration_ms, 0)
    days = safe // MS_IN_DAY
    hours = (safe % MS_IN_DAY) // MS_IN_HOUR
    minutes = (safe % MS_IN_HOUR) // MS_IN_MINUTE
    seconds = (safe % MS_IN_MINUTE) // MS_IN_SECOND
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def format_status_task_line(task: dict, label: str = "Task") -> str:
    return f"{label}: {task.get('title', '')} ({task.get('taskId', '')})"


def _read_running_task_started_at(snapshot: dict | None, task: dict) -> int | None:
    """运行中任务的"开始时刻"推导：优先最后一条 assistant.timestamp，
    退化到最后一条 user.timestamp，再退化到 task.createdAt/updatedAt。"""
    messages = snapshot.get("messages", []) if snapshot else []
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("timestamp"):
            return msg["timestamp"]
    for msg in reversed(messages):
        if msg.get("role") == "user" and msg.get("timestamp"):
            return msg["timestamp"]
    return task.get("createdAt") or task.get("updatedAt")


def read_task_worked_duration_ms(snapshot: dict | None, task: dict) -> int | None:
    """已经/正在工作的时长（毫秒）。"""
    status = task_status(task)
    if status == "running":
        start = _read_running_task_started_at(snapshot, task)
        if start is None:
            return None
        return max(int(datetime.now().timestamp() * 1000) - start, 0)
    messages = snapshot.get("messages", []) if snapshot else []
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("durationMs") is not None:
            return msg["durationMs"]
    created_at = task.get("createdAt")
    updated_at = task.get("updatedAt")
    if updated_at is not None and created_at is not None and updated_at >= created_at:
        return updated_at - created_at
    return None


def normalize_status_progress_text(value: str) -> str:
    """把连续空白压成单个空格（含 NBSP / 多空格）。"""
    if not value:
        return ""
    import re
    return re.sub(r"\s+", " ", value.strip())


def truncate_status_progress_text(value: str, max_len: int = 180) -> str:
    normalized = normalize_status_progress_text(value)
    if len(normalized) > max_len:
        return normalized[: max_len - 3] + "..."
    return normalized


def truncate_live_status_progress_text(value: str, max_len: int = 1000) -> str:
    """只保留尾部（最新），和 ZCode 原文一致。"""
    normalized = normalize_status_progress_text(value)
    if len(normalized) > max_len:
        return normalized[-max_len:]
    return normalized


def _read_status_field(value: object, keys: list[str]) -> str | None:
    if isinstance(value, str):
        return normalize_status_progress_text(value) or None
    if not isinstance(value, dict) or isinstance(value, list):
        return None
    for k in keys:
        if isinstance(value.get(k), str):
            text = normalize_status_progress_text(value[k])
            if text:
                return text
    return None


def _format_status_tool_progress(tool: dict | None) -> str | None:
    if not tool:
        return None
    name = tool.get("title") or tool.get("toolName") or tool.get("kind") or "tool"
    detail = (
        _read_status_field(tool.get("input"), ["command", "path", "file_path", "filePath", "prompt"])
        or _read_status_field(tool.get("output"), ["command", "path", "file_path", "filePath", "prompt"])
        or _read_status_field(tool.get("raw"), ["command", "path", "file_path", "filePath", "prompt"])
    )
    status = f" [{tool.get('status')}]" if tool.get("status") else ""
    return name + status + (":" + detail if detail else "")


def read_latest_task_progress(snapshot: dict | None) -> str | None:
    """最近一条 assistant 消息里的进度文本（从后往前找第一个有内容的 part）。"""
    messages = snapshot.get("messages", []) if snapshot else []
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        parts = msg.get("parts", []) or []
        for part in reversed(parts):
            ptype = part.get("type")
            if ptype in ("content", "thought") and part.get("content", "").strip():
                return truncate_status_progress_text(part["content"])
            if ptype == "tool-call":
                progress = _format_status_tool_progress(msg.get("tools", {}).get(part.get("toolIndex")))
                if progress:
                    return truncate_status_progress_text(progress)
        for key in ("content", "thought"):
            text = _read_status_field(msg.get(key), [])
            if text:
                return truncate_status_progress_text(text)
        progress = _format_status_tool_progress((msg.get("tools") or [None])[-1])
        if progress:
            return truncate_status_progress_text(progress)
    return None
