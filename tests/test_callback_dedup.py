"""
卡片回调幂等测试。

飞书按钮回调 Webhook 在超时 / 非 2xx 时会重推同一动作；本组测试覆盖:
- 首次点击正常 dispatch
- 相同三元组的重复回调被静默去重
- 不同按钮（不同 reply 值）不走 dedup
- 同一按钮在换一张卡之后依旧可以点

测试通过替换 asyncio.run_coroutine_threadsafe 捕获调度的协程，不依赖真实 event loop。
"""
import asyncio
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import main as _main
from main import P2CardActionTrigger, P2CardActionTriggerResponse


def _build_trigger(open_id="ou_test_001", open_message_id="msg_card_001",
                  value=None, action="reply"):
    """构造一个 P2CardActionTrigger"""
    return P2CardActionTrigger({
        "event": {
            "operator": {"open_id": open_id},
            "token": "ar_test_token",
            "action": {"value": value or {}},
            "context": {
                "open_message_id": open_message_id,
                "open_chat_id": "oc_test_001",
            },
        }
    })


def _capture_dispatch(dispatched: list):
    """替换 asyncio.run_coroutine_threadsafe — 捕获调度的协程对象。

    协程对象在调度后立即关闭，避免 RuntimeWarning。
    """
    def _stub(coro, loop):
        try:
            args = coro.cr_frame.f_locals if coro.cr_frame is not None else {}
            dispatched.append(dict(args))
        except Exception:
            dispatched.append({})
        coro.close()
    return _stub, _stub


# ── 三元组 key 稳定性 ────────────────────────────────────────

def test_key_stability_for_same_value():
    from callback_store import make_key
    v = {"reply": "yes", "cid": "chat_001"}
    k1 = make_key("ou_x", "msg_001", v, "reply")
    k2 = make_key("ou_x", "msg_001", v, "reply")
    assert k1 == k2


def test_key_distinguishes_different_values():
    from callback_store import make_key
    k1 = make_key("ou_x", "msg_001", {"reply": "yes"}, "reply")
    k2 = make_key("ou_x", "msg_001", {"reply": "no"}, "reply")
    assert k1 != k2


def test_key_distinguishes_different_messages():
    from callback_store import make_key
    v = {"reply": "yes"}
    k1 = make_key("ou_x", "msg_001", v, "reply")
    k2 = make_key("ou_x", "msg_002", v, "reply")
    assert k1 != k2


def test_key_distinguishes_different_users():
    from callback_store import make_key
    v = {"reply": "yes"}
    k1 = make_key("ou_a", "msg_001", v, "reply")
    k2 = make_key("ou_b", "msg_001", v, "reply")
    assert k1 != k2


def test_key_distinguishes_different_actions():
    """set_mode/run_cmd/option 即使 value 相同,action_type 不同也不应碰撞。"""
    from callback_store import make_key
    k1 = make_key("ou_x", "msg_001", {"mode": "plan"}, "set_mode")
    k2 = make_key("ou_x", "msg_001", {"mode": "plan"}, "run_cmd")
    assert k1 != k2


# ── on_card_action 去重行为 ─────────────────────────────────

def test_first_click_dispatches(monkeypatch):
    """首次点击 → 正常调度 Claude，有 toast 反馈。"""
    dispatched: list = []
    dispatch_stub, _ = _capture_dispatch(dispatched)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", dispatch_stub)

    spy = _SpyStore()
    spy.seen_calls: list = []
    spy.already_seen_result = False
    monkeypatch.setattr(_main, "_callback_store", spy)

    resp = _main.on_card_action(_build_trigger(value={"reply": "yes"}))

    assert len(dispatched) == 1  # 首次放行
    assert "yes" in str(dispatched[0]).lower() or dispatched[0]
    assert resp.toast is not None and "已发送" in resp.toast.content


def test_duplicate_callback_does_not_dispatch(monkeypatch):
    """重复回调 → dedup 命中，静默返回，不闪 toast。"""
    dispatched: list = []
    dispatch_stub, _ = _capture_dispatch(dispatched)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", dispatch_stub)

    spy = _SpyStore()
    spy.already_seen_result = True
    monkeypatch.setattr(_main, "_callback_store", spy)

    resp = _main.on_card_action(_build_trigger(value={"reply": "yes"}))

    assert dispatched == []
    assert resp.toast is None   # 静默，不误导用户


def test_admission_blocked_before_dedup(monkeypatch):
    """授权门在 dedup 之前 — 未授权用户即使首次也不该 dispatch 且不调用 dedup。"""
    dispatched: list = []
    dispatch_stub, _ = _capture_dispatch(dispatched)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", dispatch_stub)

    spy = _SpyStore()
    monkeypatch.setattr(_main, "_callback_store", spy)
    monkeypatch.setattr(_main.admission, "is_allowed", lambda uid: False)

    resp = _main.on_card_action(_build_trigger(value={"reply": "yes"}))

    assert dispatched == []
    assert spy.seen_calls == []  # 授权没过就不会走到 dedup


def test_different_button_value_passes_through(monkeypatch):
    """第二个按钮（不同 reply 值）不被 dedup 挡住。"""
    dispatched: list = []
    dispatch_stub, _ = _capture_dispatch(dispatched)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", dispatch_stub)

    spy = _SpyStore()
    seen_map: dict = {}
    def _seen(open_id, mid, value, action):
        reply = value.get("reply", "")
        seen_map[reply] = seen_map.get(reply, 0) + 1
        return seen_map[reply] > 1
    spy.already_seen_side_effect = _seen
    monkeypatch.setattr(_main, "_callback_store", spy)

    _main.on_card_action(_build_trigger(value={"reply": "yes"}))
    _main.on_card_action(_build_trigger(value={"reply": "no"}))
    _main.on_card_action(_build_trigger(value={"reply": "yes"}))  # dedup

    assert len(dispatched) == 2


def test_same_button_on_new_card_resets(monkeypatch):
    """换卡单后同一按钮还能点 — key 含 open_message_id。"""
    dispatched: list = []
    dispatch_stub, _ = _capture_dispatch(dispatched)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", dispatch_stub)

    seen = []
    def _seen(open_id, mid, value, action):
        seen.append(mid)
        return seen.count(mid) > 1
    spy = _SpyStore()
    spy.already_seen_side_effect = _seen
    monkeypatch.setattr(_main, "_callback_store", spy)

    _main.on_card_action(_build_trigger(open_message_id="card_new_001", value={"reply": "yes"}))
    _main.on_card_action(_build_trigger(open_message_id="card_new_001", value={"reply": "yes"}))
    _main.on_card_action(_build_trigger(open_message_id="card_new_002", value={"reply": "yes"}))

    assert len(dispatched) == 2


# ── run_cmd / set_mode / resume_session 分支同样过 dedup ───

def test_run_cmd_callback_dedup(monkeypatch):
    dispatched: list = []
    dispatch_stub, _ = _capture_dispatch(dispatched)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", dispatch_stub)

    spy = _SpyStore()
    spy.already_seen_result = True
    monkeypatch.setattr(_main, "_callback_store", spy)

    resp = _main.on_card_action(_build_trigger(
        value={"cmd": "/status"}, action="run_cmd"
    ))

    assert dispatched == []
    assert resp.toast is None


def test_set_mode_callback_dedup(monkeypatch):
    dispatched: list = []
    dispatch_stub, _ = _capture_dispatch(dispatched)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", dispatch_stub)

    spy = _SpyStore()
    spy.already_seen_result = True
    monkeypatch.setattr(_main, "_callback_store", spy)

    resp = _main.on_card_action(_build_trigger(
        value={"mode": "plan", "action": "set_mode"}, action="set_mode"
    ))

    assert dispatched == []


def test_resume_session_callback_dedup(monkeypatch):
    dispatched: list = []
    dispatch_stub, _ = _capture_dispatch(dispatched)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", dispatch_stub)

    spy = _SpyStore()
    spy.already_seen_result = True
    monkeypatch.setattr(_main, "_callback_store", spy)

    resp = _main.on_card_action(_build_trigger(
        value={"sid": "sid_abc", "action": "resume_session"}, action="resume_session"
    ))

    assert dispatched == []


class _SpyStore:
    """替换 main._callback_store: 可配置 already_seen 的固定返回或 side_effect。"""

    def __init__(self, already_seen_result=False):
        self.already_seen_result = already_seen_result
        self.already_seen_side_effect = None
        self.seen_calls: list = []

    def already_seen(self, open_id, mid, value, action):
        self.seen_calls.append((open_id, mid, action, dict(value)))
        if self.already_seen_side_effect is not None:
            return self.already_seen_side_effect(open_id, mid, value, action)
        return self.already_seen_result


# ── HTTP 回调路径同样过 dedup ───────────────────────────────

def test_http_callback_dedup_dispatches_and_blocks(monkeypatch):
    """HTTP 路径: 首次 dispatch，第二次被 dedup 拦掉(200 + 已处理 toast)"""
    dispatched: list = []

    spy = _SpyStore()
    seen_counts: dict = {}
    def _seen(open_id, mid, value, action):
        key = (open_id, mid, json.dumps(value, sort_keys=True))
        seen_counts[key] = seen_counts.get(key, 0) + 1
        return seen_counts[key] > 1
    spy.already_seen_side_effect = _seen

    handler = _main._CardCallbackHandler.__new__(_main._CardCallbackHandler)
    responses = []
    def _fake_respond(code, data):
        responses.append((code, data))
    handler._respond = _fake_respond

    monkeypatch.setattr(_main, "_callback_store", spy)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe",
                        lambda coro, loop: (dispatched.append(coro), coro.close()))

    body = json.dumps({
        "event": {
            "operator": {"open_id": "ou_http_001"},
            "action": {"value": {"reply": "yes"}},
            "context": {"open_message_id": "msg_h_001"},
        }
    }).encode("utf-8")

    def _do_post():
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler.do_POST()

    _do_post()
    assert len(dispatched) == 1, "首次 HTTP 回调应 dispatch"
    assert responses and responses[-1][0] == 200

    # 第二次: 同 key, dedup 命中 → 不 dispatch, 200 + 已处理 toast
    responses.clear()
    dispatched.clear()
    _do_post()
    assert dispatched == [], "重复 HTTP 回调不应 dispatch"
    assert responses and responses[-1][0] == 200
    assert any("已处理" in str(v) for _, v in responses)


def test_callback_store_persistence_key_shape(tmp_path):
    """CallbackStore 写入磁盘后重载仍可识别重复: 持久化不在内存态丢失。"""
    from callback_store import CallbackStore
    store = CallbackStore(path=str(tmp_path / "cb.json"), ttl=600)
    v = {"reply": "yes"}
    assert store.already_seen("ou_a", "msg_1", v, "reply") is False

    # 重建 store（模拟重启）: 同 key 应识别为重复
    store2 = CallbackStore(path=str(tmp_path / "cb.json"), ttl=600)
    assert store2.already_seen("ou_a", "msg_1", v, "reply") is True
    # 不同 key 仍然放行
    assert store2.already_seen("ou_a", "msg_1", {"reply": "no"}, "reply") is False
    assert store2.already_seen("ou_b", "msg_1", v, "reply") is False
