"""微信渠道测试：AES 解密、消息多形态解析、注册状态归一、
游标时机、幂等、CRLF、群命令门、runtime 锁。"""

import base64
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import weixin_client as wx
import weixin_runtime as wr


# ── AES key 三形态 ────────────────────────────────────────────


def test_parse_aes_key_hex():
    assert wx.parse_aes_key("a" * 32) == bytes.fromhex("a" * 32)


def test_parse_aes_key_base64_16bytes():
    raw = os.urandom(16)
    assert wx.parse_aes_key(base64.b64encode(raw).decode()) == raw


def test_parse_aes_key_base64_wrapping_hex():
    hex_str = "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7"
    wrapped = base64.b64encode(hex_str.encode()).decode()
    assert wx.parse_aes_key(wrapped) == bytes.fromhex(hex_str)


def test_parse_aes_key_invalid():
    assert wx.parse_aes_key("not-a-key") is None
    assert wx.parse_aes_key("") is None


def test_decrypt_cdn_media_roundtrip():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = os.urandom(16)
    plain = b"hello weixin attachment" * 10
    # PKCS7 pad
    pad = 16 - len(plain) % 16
    padded = plain + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ciphertext = enc.update(padded) + enc.finalize()
    hex_key = key.hex()
    assert wx.decrypt_cdn_media(ciphertext, hex_key) == plain
    assert wx.decrypt_cdn_media(ciphertext, base64.b64encode(key).decode()) == plain


def test_decrypt_cdn_media_bad_key():
    with pytest.raises(wx.WeixinApiError):
        wx.decrypt_cdn_media(b"\x00" * 32, "bad")


# ── 入站消息多形态 ────────────────────────────────────────────


def test_parse_inbound_flat_text():
    raw = {"message_type": 1, "text": "你好", "from_user_id": "u1", "msg_id": "m1"}
    m = wx.parse_inbound(raw)
    assert m["text"] == "你好"
    assert m["user_id"] == "u1"
    assert m["message_id"] == "m1"
    assert m["is_group"] is False


def test_parse_inbound_item_list_text():
    raw = {
        "msg": {"from_user_id": "u2", "context_token": "ctx1"},
        "item_list": [{"type": 1, "text_item": {"text": "群消息"}}],
        "room_id": "room9",
    }
    m = wx.parse_inbound(raw)
    assert m["text"] == "群消息"
    assert m["user_id"] == "u2"
    assert m["is_group"] is True
    assert m["chat_id"] == "room9"
    assert m["context_token"] == "ctx1"


def test_parse_inbound_bot_message_filtered():
    assert wx.parse_inbound({"message_type": 2, "text": "self", "from_user_id": "bot"}) is None


def test_parse_inbound_no_user_id_rejected():
    assert wx.parse_inbound({"text": "x"}) is None


def test_parse_inbound_user_id_forms():
    assert wx.parse_inbound({"text": "t", "from": {"wxid": "w1"}})["user_id"] == "w1"
    assert wx.parse_inbound({"text": "t", "sender": {"id": "s1"}})["user_id"] == "s1"
    assert wx.parse_inbound({"text": "t", "userId": "u9"})["user_id"] == "u9"


def test_image_item_full_url_and_mid_size():
    raw = {
        "text": "",
        "from_user_id": "u1",
        "item_list": [{
            "type": 2,
            "image_item": {
                "media": {"md5": "abc123"},
                "full_url": "https://cdn.example.com/img",
                "mid_size": 20480,
                "thumb_size": 2048,
                "aes_key": "a" * 32,
            },
        }],
    }
    m = wx.parse_inbound(raw)
    assert len(m["attachments"]) == 1
    att = m["attachments"][0]
    assert att["kind"] == "image"
    assert att["download_url"] == "https://cdn.example.com/img"
    assert att["size_bytes"] == 20480  # mid_size 是实际大小，不是 thumb_size
    assert att["aes_key"] == "a" * 32


def test_direct_attachments_form():
    raw = {
        "text": "",
        "from": "u1",
        "attachments": [{"kind": "file", "id": "f1", "filename": "a.pdf",
                         "downloadUrl": "https://cdn/x", "aesKey": "b" * 32}],
    }
    m = wx.parse_inbound(raw)
    assert m["attachments"][0]["kind"] == "file"
    assert m["attachments"][0]["download_url"] == "https://cdn/x"
    assert m["attachments"][0]["aes_key"] == "b" * 32


def test_infer_attachment_kind():
    assert wx.infer_attachment_kind({"image_item": {}}) == "image"
    assert wx.infer_attachment_kind({"mime_type": "video/mp4"}) == "video"
    assert wx.infer_attachment_kind({"filename": "song.mp3"}) == "audio"
    assert wx.infer_attachment_kind({"filename": "doc.pdf"}) == "file"


# ── 消息容器 / 游标兼容 ────────────────────────────────────────


def test_read_messages_container_forms():
    for key in ("msgs", "messages", "updates", "items", "list"):
        assert len(wx._read_messages_container({"data": {key: [{"text": "x", "from_user_id": "u"}]}})) == 1
    assert len(wx._read_messages_container({"data": {"msgs": {"text": "x", "from_user_id": "u"}}})) == 1


def test_read_next_buf_aliases():
    for key in ("get_updates_buf", "buf", "next_buf", "nextBuf", "getUpdatesBuf", "syncKey"):
        assert wx._read_next_buf({"data": {key: "B1"}}) == "B1"


def test_summarize_raw_message_shapes_only():
    raw = {"text": "secret content", "from_user_id": "u1",
           "item_list": [{"type": 1, "text_item": {"text": "secret"}}]}
    diag = wx.summarize_raw_message(raw, 0)
    assert "secret" not in diag  # 只记形状不记正文
    assert "textLen=" in diag and "itemTypes=" in diag


# ── 注册状态归一 ───────────────────────────────────────────────


def test_normalize_qr_status_numeric():
    assert wx.normalize_qr_status(0) == "pending"
    assert wx.normalize_qr_status(1) == "scanned"
    assert wx.normalize_qr_status(2) == "success"
    assert wx.normalize_qr_status(3) == "expired"
    assert wx.normalize_qr_status(4) == "expired"


def test_normalize_qr_status_string_aliases():
    assert wx.normalize_qr_status("CONFIRMED") == "success"
    assert wx.normalize_qr_status("scaned") == "scanned"
    assert wx.normalize_qr_status("timeout") == "expired"
    assert wx.normalize_qr_status("weird") == "pending"
    assert wx.normalize_qr_status(None) == "pending"


# ── 游标时机与 runtime ────────────────────────────────────────


class _FakeClient:
    """按 buf 返回批次：游标未推进时服务端会重放同一批——正确模拟长轮询语义。"""
    def __init__(self):
        self.sent = []

    def get_updates(self, buf=""):
        if buf == "":
            return ([{"text": "a"}, {"text": "b"}], [], "B2")
        if buf == "B2":
            return ([{"text": "c"}], [], "B3")
        return [], [], buf

    def send_text(self, to, text, from_user_id="", context_token=""):
        self.sent.append((to, text))


def test_runtime_writes_buf_only_after_processing(tmp_path):
    """游标必须在本批全部处理完后才写盘；处理函数抛错时游标不推进。"""
    buf_path = tmp_path / "buf.json"
    processed = []
    fail_on = {"first": True}

    def on_message(m):
        if fail_on["first"]:
            fail_on["first"] = False
            raise RuntimeError("mid-batch failure")
        processed.append(m["text"])

    runtime = wr.WeixinRuntime("tok", "bot", str(buf_path), on_message)
    runtime._lock = _FakeLock()
    client = _FakeClient()
    # 第一轮：批内第一条抛错 → 异常退避，buf 不写
    with pytest.raises(RuntimeError):
        runtime._poll_once(client)
    assert not buf_path.exists()
    # 第二轮：正常处理 → 处理完成后 buf 才写为 B2
    runtime._poll_once(client)
    assert processed == ["a", "b"]
    assert json.loads(buf_path.read_text(encoding="utf-8"))["buf"] == "B2"


class _FakeLock:
    def acquire(self):
        return True

    def release(self):
        pass


def test_runtime_read_write_buf_atomic(tmp_path):
    buf_path = tmp_path / "sub" / "buf.json"
    runtime = wr.WeixinRuntime("tok", "bot", str(buf_path), on_message=lambda m: None)
    assert runtime._read_buf() == ""
    runtime._write_buf("B1")
    assert runtime._read_buf() == "B1"
    assert buf_path.exists() and not str(buf_path).endswith(".tmp")


def test_runtime_fingerprint_changes_with_token():
    a = wr.WeixinRuntime("tok1", "bot", "x", on_message=lambda m: None)
    b = wr.WeixinRuntime("tok2", "bot", "x", on_message=lambda m: None)
    assert a.fingerprint != b.fingerprint


# ── 目录锁 ────────────────────────────────────────────────────


def test_lock_acquire_release(tmp_path):
    lock = wr.WeixinPollingLock(str(tmp_path), "weixin:tok")
    assert lock.acquire() is True
    assert os.path.isdir(lock.path)
    owner = json.load(open(os.path.join(lock.path, "owner.json"), encoding="utf-8"))
    assert owner["pid"] == os.getpid()
    lock.release()
    assert not os.path.isdir(lock.path)


def test_lock_second_acquire_fails_then_release(tmp_path):
    lock1 = wr.WeixinPollingLock(str(tmp_path), "k")
    assert lock1.acquire() is True
    lock2 = wr.WeixinPollingLock(str(tmp_path), "k")
    assert lock2.acquire() is False  # 持锁进程存活且心跳新鲜 → 不接管
    lock1.release()
    assert lock2.acquire() is True
    lock2.release()


def test_lock_takes_over_dead_process(tmp_path):
    lock1 = wr.WeixinPollingLock(str(tmp_path), "k")
    assert lock1.acquire() is True
    # 伪造 owner pid 为不存在的进程
    owner_path = os.path.join(lock1.path, "owner.json")
    owner = json.load(open(owner_path, encoding="utf-8"))
    owner["pid"] = 999999999
    json.dump(owner, open(owner_path, "w", encoding="utf-8"))
    lock2 = wr.WeixinPollingLock(str(tmp_path), "k")
    assert lock2.acquire() is True  # 进程死亡 → stale 接管
    lock2.release()


def test_lock_takes_over_expired_lease(tmp_path):
    lock1 = wr.WeixinPollingLock(str(tmp_path), "k")
    assert lock1.acquire() is True
    # 心跳文件 mtime 拨回超过 LEASE_SEC
    lease = os.path.join(lock1.path, "lease")
    old = time.time() - wr.LOCK_LEASE_SEC - 10
    os.utime(lease, (old, old))
    lock2 = wr.WeixinPollingLock(str(tmp_path), "k")
    assert lock2.acquire() is True
    lock2.release()


def test_lock_release_verifies_owner(tmp_path):
    """释放校验 nonce：后来接管者的锁不被前一个持有者误删。"""
    lock1 = wr.WeixinPollingLock(str(tmp_path), "k")
    assert lock1.acquire() is True
    # 他人接管（绕过 stale 检查直接改 owner）
    owner_path = os.path.join(lock1.path, "owner.json")
    json.dump({"pid": os.getpid(), "nonce": "other", "createdAt": 0},
              open(owner_path, "w", encoding="utf-8"))
    lock1.release()
    assert os.path.isdir(lock1.path)  # nonce 不匹配 → 不删


# ── 协议层：发送文本 CRLF ──────────────────────────────────────


def test_send_text_normalizes_crlf():
    client = wx.WeixinClient.__new__(wx.WeixinClient)
    client.token = "t"
    client.base_url = "https://x"
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["body"] = json.loads(data)
        class R:
            status_code = 200
            def json(self):
                return {"ret": 0}
        return R()

    orig = wx.requests.post
    wx.requests.post = fake_post
    try:
        client.send_text("u1", "line1\nline2\r\nline3")
    finally:
        wx.requests.post = orig
    msg = captured["body"]["msg"]
    assert msg["item_list"][0]["text_item"]["text"] == "line1\r\nline2\r\nline3"
    assert msg["to_user_id"] == "u1"
    assert msg["message_type"] == 2
    assert captured["body"]["base_info"]["channel_version"] == "2.0.0"


def test_request_json_error_raises(monkeypatch):
    client = wx.WeixinClient("t")
    class R:
        status_code = 200
        def json(self):
            return {"ret": -1, "errmsg": "bad token"}
    monkeypatch.setattr(wx.requests, "post", lambda *a, **k: R())
    with pytest.raises(wx.WeixinApiError, match="bad token"):
        client.test()


def test_build_headers():
    headers = wx.build_headers("tok")
    assert headers["Authorization"] == "Bearer tok"
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert base64.b64decode(headers["X-WECHAT-UIN"]).decode().isdigit()


# ── 管线：幂等 / CRLF / 群命令门（走 main.handle_weixin_message_async）──


class _FakeFeishuStore:
    def __init__(self):
        self.sessions = {}
        self.responses = []
        self.modes = {}

    async def get_current(self, user_id, chat_id):
        from session_store import Session
        return Session(session_id="", model="m", cwd=os.getcwd(),
                       permission_mode="bypassPermissions")

    async def on_claude_response(self, user_id, chat_id, sid, first):
        self.responses.append((user_id, chat_id, sid))

    async def set_permission_mode(self, user_id, chat_id, mode):
        self.modes[(user_id, chat_id)] = mode


def test_weixin_keys_namespace_isolation():
    from main import _wx_keys
    uid, owner, store_owner, is_group = _wx_keys({"user_id": "u1", "chat_id": "room1"})
    assert (uid, owner, is_group) == ("u1", "room1", True)
    assert store_owner == "wx:room1"
    _uid, owner2, store2, group2 = _wx_keys({"user_id": "u1", "chat_id": ""})
    assert (owner2, store2, group2) == ("u1", "wx:u1", False)


def test_weixin_dedup_uses_wx_prefix(monkeypatch):
    """幂等键带 wx: 前缀——与飞书 message_id 命名空间隔离。"""
    import main
    seen = set()
    monkeypatch.setattr(main, "_is_duplicate", lambda k: k in seen)
    seen.add("wx:m1")
    inbound = {"text": "/stop", "user_id": "u1", "chat_id": "", "message_id": "m1"}
    sent = []
    monkeypatch.setattr(main, "_weixin_client", type("C", (), {
        "send_text": staticmethod(lambda *a, **k: sent.append(a)),
        "send_typing": staticmethod(lambda *a, **k: None),
    })())
    import asyncio
    asyncio.run(main.handle_weixin_message_async(inbound))
    assert sent == []  # 重复消息被丢弃，/stop 未执行
