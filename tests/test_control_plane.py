"""控制层验证：授权门、幂等、并发上限、默认不打断、安静模式、结构化选项。"""
import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

import main
from admission import Admission
from seen_store import SeenStore


def _event(user="ou_admin", chat=None, chat_type="p2p", text="干活", mid="msg_ctl_1", mentions=None):
    e = MagicMock()
    e.event.sender.sender_id.open_id = user
    e.event.message.chat_type = chat_type
    e.event.message.chat_id = chat or user
    e.event.message.message_type = "text"
    e.event.message.content = json.dumps({"text": text})
    e.event.message.message_id = mid
    e.event.message.mentions = mentions
    return e


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    main._seen_store.clear()
    main._chat_locks.clear()
    monkeypatch.setattr(main.config, "INTERRUPT_ON_NEW_MESSAGE", False)
    monkeypatch.setattr(main.config, "QUIET_MODE", False)
    yield
    main._seen_store.clear()
    main._chat_locks.clear()


# ── 授权门 ──────────────────────────────────────────────────

def test_admission_auto_locks_first_p2p_sender(tmp_path):
    adm = Admission("auto", state_file=str(tmp_path / "adm.json"))
    allowed, reason = adm.check("ou_first", is_group=False, is_addressed=True)
    assert allowed and reason == "first-admin-locked"
    assert adm.check("ou_first", False, True)[0] is True
    assert adm.check("ou_other", False, True)[0] is False
    assert adm.allowed_ids() == ["ou_first"]


def test_admission_persists_across_restart(tmp_path):
    path = str(tmp_path / "adm.json")
    Admission("auto", state_file=path).check("ou_first", False, True)
    restarted = Admission("auto", state_file=path)
    assert restarted.is_allowed("ou_first") is True
    assert restarted.is_allowed("ou_other") is False


def test_admission_observe_records_but_never_executes(tmp_path):
    adm = Admission("observe", state_file=str(tmp_path / "adm.json"))
    assert adm.check("ou_first", False, True)[0] is False
    assert adm.allowed_ids() == []


def test_admission_group_cannot_bind_admin(tmp_path):
    adm = Admission("auto", state_file=str(tmp_path / "adm.json"))
    assert adm.check("ou_group", is_group=True, is_addressed=True)[0] is False
    assert adm.allowed_ids() == []


def test_admission_explicit_admin_wins(tmp_path):
    adm = Admission("auto", explicit_admin="ou_boss", state_file=str(tmp_path / "adm.json"))
    assert adm.check("ou_first", False, True)[0] is False
    assert adm.check("ou_boss", False, True)[0] is True


def test_admission_off_allows_everything():
    assert Admission("off").check("ou_any", False, True)[0] is True


@pytest.mark.asyncio
async def test_unauthorized_message_cannot_start_cli(monkeypatch):
    monkeypatch.setattr(main, "admission", Admission("auto", explicit_admin="ou_boss"))
    with patch.object(main, "_process_message", new_callable=AsyncMock) as proc:
        await main.handle_message_async(_event(user="ou_intruder", mid="msg_unauth"))
        proc.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_message_starts_cli(monkeypatch):
    monkeypatch.setattr(main, "admission", Admission("auto", explicit_admin="ou_boss"))
    with patch.object(main, "_process_message", new_callable=AsyncMock) as proc:
        await main.handle_message_async(_event(user="ou_boss", mid="msg_auth"))
        proc.assert_awaited_once()


# ── 幂等 ────────────────────────────────────────────────────

def test_duplicate_message_id_is_ignored():
    assert main._is_duplicate("msg_dup") is False
    assert main._is_duplicate("msg_dup") is True
    assert main._is_duplicate("msg_other") is False


def test_seen_ids_bounded():
    cap = main._seen_store.max_items
    for i in range(cap + 50):
        main._is_duplicate(f"msg_{i}")
    assert main._seen_store.size() == cap


# ── 幂等表持久化：重启后不得重放 ─────────────────────────────

def test_seen_store_survives_restart(tmp_path):
    path = str(tmp_path / "seen.json")
    s1 = SeenStore(path=path, ttl=600, max_items=100)
    assert s1.seen("msg_x") is False
    s1.flush()

    s2 = SeenStore(path=path, ttl=600, max_items=100)
    assert s2.seen("msg_x") is True, "重启后同一 message_id 必须仍判为重复"
    assert s2.seen("msg_y") is False


def test_seen_store_expires_by_ttl(tmp_path):
    path = str(tmp_path / "seen.json")
    s1 = SeenStore(path=path, ttl=10, max_items=100)
    assert s1.seen("msg_old") is False
    s1.flush()

    # 模拟时间流逝：直接改磁盘上的时间戳，绕过真实 sleep
    import json as _json
    data = _json.load(open(path, encoding="utf-8"))
    data["msg_old"] = data["msg_old"] - 100
    _json.dump(data, open(path, "w", encoding="utf-8"))

    s2 = SeenStore(path=path, ttl=10, max_items=100)
    assert s2.seen("msg_old") is False, "超过 TTL 的记录必须过期，重新视为首次"


def test_seen_store_survives_corrupt_file(tmp_path):
    path = str(tmp_path / "seen.json")
    path_obj = __import__("pathlib").Path(path)
    path_obj.write_text("{ this is not json", encoding="utf-8")

    s = SeenStore(path=path, ttl=600, max_items=100)
    assert s.size() == 0
    assert s.seen("msg_ok") is False


def test_replayed_event_after_restart_does_not_run_twice(monkeypatch, tmp_path):
    """核心场景：watchdog 重启后飞书重推事件，不能跑两遍。"""
    import importlib
    path = str(tmp_path / "seen.json")
    monkeypatch.setenv("SEEN_STORE_PATH", path)

    store1 = SeenStore(path=path, ttl=600, max_items=100)
    monkeypatch.setattr(main, "_seen_store", store1)
    with patch.object(main, "_process_message", new_callable=AsyncMock) as proc:
        asyncio.run(main.handle_message_async(_event(mid="msg_reboot")))
        proc.assert_awaited_once()

    # 模拟进程重启：全新 SeenStore 从磁盘加载
    store2 = SeenStore(path=path, ttl=600, max_items=100)
    monkeypatch.setattr(main, "_seen_store", store2)
    with patch.object(main, "_process_message", new_callable=AsyncMock) as proc:
        asyncio.run(main.handle_message_async(_event(mid="msg_reboot")))
        proc.assert_not_awaited(), "重启后重放的事件必须被幂等挡掉"


@pytest.mark.asyncio
async def test_replayed_event_does_not_run_twice():
    with patch.object(main, "_process_message", new_callable=AsyncMock) as proc:
        await main.handle_message_async(_event(mid="msg_replay"))
        await main.handle_message_async(_event(mid="msg_replay"))
        proc.assert_awaited_once()


# ── 并发上限 / 默认不打断 ────────────────────────────────────

@pytest.mark.asyncio
async def test_global_run_slots_cap(monkeypatch):
    monkeypatch.setattr(main, "MAX_CONCURRENT_RUNS", 1)
    main._run_slots = None
    entered, peak = 0, 0
    lock = asyncio.Lock()

    async def fake_process(*args, **kwargs):
        nonlocal entered, peak
        async with lock:
            entered += 1
            peak = max(peak, entered)
        await asyncio.sleep(0.02)
        async with lock:
            entered -= 1

    with patch.object(main, "_process_message", side_effect=fake_process):
        await asyncio.gather(*[
            main.handle_message_async(_event(user=f"ou_{i}", chat=f"chat_{i}", mid=f"msg_cap_{i}"))
            for i in range(4)
        ])
    assert peak == 1


@pytest.mark.asyncio
async def test_new_message_does_not_kill_running_task_by_default():
    active = MagicMock(stop_requested=False)
    with patch.object(main._active_runs, "get_run", return_value=active), \
         patch.object(main, "stop_run", new_callable=AsyncMock) as stop, \
         patch.object(main, "_process_message", new_callable=AsyncMock):
        await main.handle_message_async(_event(mid="msg_no_interrupt"))
        stop.assert_not_awaited()


# ── 注意力纪律 ──────────────────────────────────────────────

def test_truncate_reply_keeps_tail():
    long_text = "前" * (main.config.REPLY_MAX_CHARS + 100) + "结论"
    out = main._truncate_reply(long_text)
    assert out.endswith("结论") and len(out) < len(long_text)


def test_options_from_structured_ask_user_input():
    inp = {"questions": [{"question": "继续吗？", "options": [
        {"label": "继续", "description": "接着做"},
        {"label": "停止", "description": "到此为止"},
    ]}]}
    assert main._options_from_ask_user(inp) == [("继续 · 接着做", "继续"), ("停止 · 到此为止", "停止")]


def test_handover_requires_control_token(monkeypatch):
    monkeypatch.setattr(main.config, "CONTROL_TOKEN", "")
    handler = main._CardCallbackHandler.__new__(main._CardCallbackHandler)
    responses = []
    handler._respond = lambda code, data: responses.append((code, data))
    handler.path = "/handover?session_id=sid_abc"
    handler.do_GET()
    assert responses == [(403, {"error": "handover disabled: set CONTROL_TOKEN"})]
