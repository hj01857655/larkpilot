"""
全局 test fixtures。
确保所有测试使用临时目录存储 sessions，不污染 ~/.feishu-claude/sessions.json。
"""

import os

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")
# 测试一律从 off 起步：本地 .env 里的 ADMISSION_MODE=auto + 真实 open_id
# 会随 load_dotenv() 进入进程，把夹具里的假 sender 全部拒掉
os.environ["ADMISSION_MODE"] = "off"
os.environ.pop("ADMIN_OPEN_ID", None)
os.environ["INTERRUPT_ON_NEW_MESSAGE"] = "false"
os.environ["QUIET_MODE"] = "false"

import session_store as _ss
import main as _main
from admission import Admission


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    """自动隔离: 将 SESSIONS_DIR / SESSIONS_FILE 指向临时目录"""
    monkeypatch.setattr(_ss, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(_ss, "SESSIONS_FILE", str(tmp_path / "sessions.json"))


@pytest.fixture(autouse=True)
def _isolate_dedup(monkeypatch, tmp_path):
    """每个用例把幂等表指向 tmp_path 独立实例（不污染 ~/.feishu-claude/seen_messages.json），
    并把授权门固定为 off（本地 .env 可能是 auto）。群命令门同理关闭，
    否则群命令测试会把假成员锁定写进真实 admission.json。"""
    from seen_store import SeenStore
    store = SeenStore(path=str(tmp_path / "seen.json"), ttl=600, max_items=2000)
    monkeypatch.setattr(_main, "_seen_store", store)
    monkeypatch.setattr(_main, "admission", Admission("off"))
    import bot_config as _cfg
    monkeypatch.setattr(_cfg, "GROUP_ADMIN_ONLY", "off")
    yield
