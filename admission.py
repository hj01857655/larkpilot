"""
授权门：决定一条飞书消息能否让本机 claude CLI 动手。

规则由本模块强制执行，不写进 prompt —— 消息内容和引用文档只是数据，
不能改变权限（dsh-superagent docs/002 的同一条边界）。

状态落在 ~/.feishu-claude/admission.json：bot 每 4 小时定时重启，
管理员只存在内存里的话，重启后任何人都可能抢先私聊成为管理员。
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone

STATE_FILE = os.path.expanduser("~/.feishu-claude/admission.json")
_LOCK = threading.Lock()


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save(path: str, data: dict) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Admission:
    """mode=off 全放行；observe 只记录不执行；auto 按 allowlist 放行或锁定首个私聊者。"""

    def __init__(self, mode: str, explicit_admin: str = "", state_file: str = STATE_FILE):
        self.mode = mode if mode in ("off", "observe", "auto") else "off"
        self.explicit_admin = explicit_admin.strip()
        self.state_file = state_file

    def _read_admin(self) -> str:
        if self.explicit_admin:
            return self.explicit_admin
        return str(_load(self.state_file).get("admin_open_id", "") or "")

    def _write_admin(self, open_id: str) -> None:
        data = _load(self.state_file)
        data["admin_open_id"] = open_id
        data["locked_at"] = datetime.now(timezone.utc).isoformat()
        _save(self.state_file, data)

    def check(self, sender_open_id: str, is_group: bool, is_addressed: bool) -> tuple[bool, str]:
        """返回 (是否允许执行, 原因)。调用方只对允许者起 CLI 任务。"""
        if self.mode == "off":
            return True, "off"
        if not sender_open_id:
            return False, "no-sender"

        with _LOCK:
            admin = self._read_admin()
            if admin:
                if sender_open_id == admin:
                    return True, "admin"
                return False, "non-admin" if self.mode == "auto" else "observe:non-admin"

            # 未锁定管理员：只认第一个私聊用户的显式寻址，群聊一律忽略
            if is_group or not is_addressed:
                return False, "unbound-group-or-unaddressed"
            if self.mode == "observe":
                return False, "observe:would-lock"
            self._write_admin(sender_open_id)
            return True, "first-admin-locked"

    def allowed_ids(self) -> list[str]:
        admin = self._read_admin()
        return [admin] if admin else []

    def is_allowed(self, sender_open_id: str) -> bool:
        """回调路径用：只判断"是否已是授权者"，不触发首次锁定（按钮/HTTP 不绑定管理员）。"""
        if self.mode == "off":
            return True
        if not sender_open_id:
            return False
        admin = self._read_admin()
        return bool(admin) and sender_open_id == admin

    def reset(self) -> None:
        """人工解除锁定（供 /admission 命令或手工维护使用）。"""
        with _LOCK:
            data = _load(self.state_file)
            data.pop("admin_open_id", None)
            data.pop("locked_at", None)
            _save(self.state_file, data)

    # ── 群命令门（照 ZCode groupOwnerOnly 语义）────────────────

    def group_command_allowed(self, chat_id: str, open_id: str, mode: str = "auto") -> tuple[bool, str]:
        """群内写命令（new/resume/model/mode/ws/cd）只归群管理员。

        管理员推导顺序：显式 ADMIN_OPEN_ID → admission 全局 admin → 该群
        首个执行命令的成员（独立落盘，锁定后重启不丢）。mode=off 全放行。
        """
        if mode == "off":
            return True, "off"
        if not open_id or not chat_id:
            return False, "no-identity"

        with _LOCK:
            data = _load(self.state_file)

            explicit = self.explicit_admin
            global_admin = explicit or str(data.get("admin_open_id", "") or "")
            if global_admin:
                if open_id == global_admin:
                    return True, "global-admin"
                return False, "not-group-admin"

            admins = data.setdefault("group_admins", {})
            locked = str(admins.get(chat_id, "") or "")
            if locked:
                if open_id == locked:
                    return True, "group-admin"
                return False, "not-group-admin"

            # 首个执行命令的群成员锁定为该群管理员
            admins[chat_id] = open_id
            data["group_admins_locked_at"] = data.get("group_admins_locked_at") or {}
            data["group_admins_locked_at"][chat_id] = datetime.now(timezone.utc).isoformat()
            _save(self.state_file, data)
            return True, "first-group-admin-locked"
