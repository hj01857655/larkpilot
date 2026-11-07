"""
微信 iLink Bot 渠道协议客户端（照 ZCode weixinProvider.ts / weixinRegistration.ts 全量语义）。

协议要点：
- 端点 POST JSON 到 https://ilinkai.weixin.qq.com/ilink/bot/*；鉴权头
  Authorization: Bearer <token> + AuthorizationType: ilink_bot_token，
  X-WECHAT-UIN 为随机 uint32 的 base64（服务端仅要求字段存在）。
- 所有请求体带 base_info.channel_version；响应 ret/errcode 非 0 视为失败。
- 收消息走 /getupdates 长轮询，游标 get_updates_buf 必须在本批消息全部
  处理完成后才允许持久化——提前写会让中途失败的消息被服务端视为已消费。
- message_type == 2 是 bot 自己发出的消息，入站必须丢弃防自循环。
- 发消息走 /sendmessage；文本统一 CRLF（不同客户端对 LF 折行处理不一致）。
- 附件：CDN 下载后是 AES-128-ECB + PKCS7 密文，用消息内 aes_key 解密；
  key 兼容 32 位 hex 与 base64（解码后 16 字节）两种形态。
"""

import base64
import json
import random
import re
import uuid

import requests

DEFAULT_WEIXIN_ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_BOT_API_PREFIX = "/ilink/bot"
WEIXIN_CHANNEL_VERSION = "2.0.0"
WEIXIN_MESSAGE_TYPE_BOT = 2
WEIXIN_MESSAGE_STATE_FINISH = 2
WEIXIN_GET_UPDATES_TIMEOUT_SEC = 90
WEIXIN_LOGIN_REQUEST_TIMEOUT_SEC = 30
WEIXIN_LOGIN_INTERVAL_SEC = 3
WEIXIN_LOGIN_EXPIRE_SEC = 120

# 入站附件 kind 上限对齐飞书管线：image/file 喂 Claude，audio/video 明确拒收
SUPPORTED_ATTACHMENT_KINDS = ("image", "file", "audio", "video")


class WeixinApiError(Exception):
    """iLink 接口返回 ret/errcode 非 0 或 HTTP 非 2xx。"""


def _is_record(value) -> bool:
    return isinstance(value, dict)


def _read_string(record, key: str) -> str:
    value = record.get(key) if record else None
    return value if isinstance(value, str) else ""


def _read_number(record, key):
    value = record.get(key) if record else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _read_number_or_string(record, key: str) -> str:
    value = record.get(key) if record else None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def parse_aes_key(value: str):
    """aes_key 三形态归一：32 位 hex 直用；base64 解出 16 字节直用；
    base64 解出文本再匹配 hex 则二次解码。均不匹配返回 None。"""
    trimmed = (value or "").strip()
    if re.fullmatch(r"[a-fA-F0-9]{32}", trimmed):
        return bytes.fromhex(trimmed)
    try:
        decoded = base64.b64decode(trimmed, validate=True)
    except Exception:
        return None
    if len(decoded) == 16:
        return decoded
    try:
        text = decoded.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if re.fullmatch(r"[a-fA-F0-9]{32}", text):
        return bytes.fromhex(text)
    return None


def decrypt_cdn_media(data: bytes, aes_key: str) -> bytes:
    """微信 CDN 附件是 AES-128-ECB + PKCS7 padding 的密文字节；
    直接保存会得到不可识别的文件，必须先解密。"""
    key = parse_aes_key(aes_key)
    if not key:
        raise WeixinApiError("Weixin attachment AES key is invalid.")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: PLC0415 延迟导入，未用附件功能时不强制依赖
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    if not padded or not 1 <= padded[-1] <= 16:
        raise WeixinApiError("Weixin attachment decryption failed: bad PKCS7 padding.")
    return padded[:-padded[-1]]


def build_random_wechat_uin() -> str:
    return base64.b64encode(str(random.randint(0, 0xFFFFFFFF)).encode("utf-8")).decode("ascii")


def build_headers(token: str) -> dict:
    return {
        "content-type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": build_random_wechat_uin(),
    }


def _append_base_info(body):
    if not _is_record(body):
        return body
    return {"base_info": {"channel_version": WEIXIN_CHANNEL_VERSION}, **body}


class WeixinClient:
    """iLink Bot HTTP 客户端。同步 requests——轮询在专用线程内长阻塞。"""

    def __init__(self, token: str, base_url: str = ""):
        self.token = (token or "").strip()
        self.base_url = (base_url or DEFAULT_WEIXIN_ILINK_BASE_URL).rstrip("/")

    def _request_json(self, path: str, body=None, timeout: float = 30.0):
        if not self.token:
            raise WeixinApiError(
                "Weixin iLink bot token is missing. Scan the Weixin login QR code first."
            )
        url = f"{self.base_url}{WEIXIN_BOT_API_PREFIX}{path}"
        try:
            resp = requests.post(
                url,
                headers=build_headers(self.token),
                data=json.dumps(_append_base_info(body or {})),
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise WeixinApiError(f"Weixin iLink {path} transport error: {e}") from e
        if resp.status_code >= 300:
            raise WeixinApiError(f"Weixin iLink {path} failed: HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise WeixinApiError(f"Weixin iLink {path} returned non-JSON body") from e
        data = payload if _is_record(payload) else {}
        ret = _read_number(data, "ret")
        errcode = _read_number(data, "errcode")
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            message = (
                _read_string(data, "errmsg")
                or _read_string(data, "message")
                or f"ret={'' if ret is None else ret} errcode={'' if errcode is None else errcode}".strip()
            )
            raise WeixinApiError(f"Weixin iLink {path} failed: {message}")
        return payload

    # ── 收消息（长轮询）───────────────────────────────────────

    def get_updates(self, buf: str = ""):
        """长轮询一批消息。返回 (messages, diagnostics, next_buf)。
        next_buf 为空时调用方沿用旧游标。"""
        payload = self._request_json(
            "/getupdates",
            {"get_updates_buf": buf or ""},
            timeout=WEIXIN_GET_UPDATES_TIMEOUT_SEC + 10,
        )
        container = _unwrap_data(payload)
        raw_messages = _read_messages_container(container)
        messages = [m for m in (parse_inbound(r) for r in raw_messages) if m]
        diagnostics = [summarize_raw_message(r, i) for i, r in enumerate(raw_messages)]
        next_buf = _read_next_buf(container)
        return messages, diagnostics, (next_buf or buf or "")

    def test(self) -> str:
        """连通性检查：/getconfig 无参数调用。"""
        self._request_json("/getconfig", {})
        return "Weixin iLink API is reachable."

    # ── 发消息 / typing ──────────────────────────────────────

    def send_text(self, to_user_id: str, text: str,
                  from_user_id: str = "", context_token: str = "") -> None:
        """发送纯文本。iLink 协议必须走 /sendmessage 且文本放 msg.item_list；
        不同客户端对 LF 的折行处理不一致，统一转 CRLF。"""
        body_text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        msg = {
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
            "client_id": f"larkpilot-weixin-{uuid.uuid4()}",
            "message_type": WEIXIN_MESSAGE_TYPE_BOT,
            "message_state": WEIXIN_MESSAGE_STATE_FINISH,
            "item_list": [{"type": 1, "text_item": {"text": body_text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        self._request_json("/sendmessage", {"msg": msg})

    def send_typing(self, ilink_user_id: str, context_token: str = "") -> None:
        """输入状态两段式：/getconfig 拿 typing_ticket，再 /sendtyping。
        服务端不下发 typing_ticket 时静默跳过（非关键路径）。"""
        try:
            config = self._request_json("/getconfig", {
                "ilink_user_id": ilink_user_id,
                **({"context_token": context_token} if context_token else {}),
            })
            ticket = _read_string(config if _is_record(config) else {}, "typing_ticket")
            data = config.get("data") if _is_record(config) and _is_record(config.get("data")) else {}
            ticket = ticket or _read_string(data, "typing_ticket")
            if not ticket:
                return
            self._request_json("/sendtyping", {
                "ilink_user_id": ilink_user_id,
                "typing_ticket": ticket,
                "status": 1,
            })
        except WeixinApiError:
            pass  # typing 是体验增强，失败不影响消息收发

    # ── 附件 ─────────────────────────────────────────────────

    def download_attachment(self, download_url: str, aes_key: str) -> bytes:
        if not download_url or not aes_key:
            raise WeixinApiError("Weixin attachment needs both downloadUrl and aes_key.")
        resp = requests.get(download_url, timeout=60)
        if resp.status_code >= 300:
            raise WeixinApiError(f"Weixin attachment download failed: HTTP {resp.status_code}")
        return decrypt_cdn_media(resp.content, aes_key)

    # ── webhook 回调入站（另一条接收路径）─────────────────────

    def parse_callback_payload(self, payload) -> list:
        """Webhook 推送的载荷解析。botId 是唯一必填标识，缺失即整体丢弃。"""
        if not _is_record(payload):
            return []
        bot_id = _read_string(payload, "botId")
        if not bot_id:
            return []
        return [m for m in (parse_inbound(r) for r in _read_messages_container(payload)) if m]


# ── 入站消息解析（多字段形态兼容，照 ZCode read* 函数族）─────────


def _unwrap_data(payload):
    if _is_record(payload) and "data" in payload:
        return payload["data"]
    return payload


def _read_messages_container(payload) -> list:
    data = _unwrap_data(payload)
    if not _is_record(data):
        return []
    for key in ("msgs", "messages", "updates", "items", "list"):
        raw = data.get(key)
        if isinstance(raw, list):
            return [r for r in raw if _is_record(r)]
        if _is_record(raw):
            return [raw]
    return []


def _read_next_buf(container) -> str:
    container = _unwrap_data(container)
    for key in ("get_updates_buf", "buf", "next_buf", "nextBuf", "getUpdatesBuf", "syncKey"):
        value = _read_string(container, key)
        if value:
            return value
    return ""


def _read_text_item(item) -> str:
    if not _is_record(item):
        return ""
    text_item = item.get("text_item") if _is_record(item.get("text_item")) else {}
    return (
        _read_string(text_item, "text")
        or _read_string(item, "text")
        or _read_string(item, "content")
    )


def _read_inner(message):
    """iLink 载荷有两种嵌套：扁平 msg 字段，或 message/msg 内层对象。"""
    for key in ("msg", "message"):
        inner = message.get(key)
        if _is_record(inner):
            return inner
    return None


def _item_list_of(message) -> list:
    inner = _read_inner(message)
    for source in (message, inner):
        if source and isinstance(source.get("item_list"), list):
            return source["item_list"]
    return []


def _direct_attachments_of(message) -> list:
    inner = _read_inner(message)
    for source in (message, inner):
        if source and isinstance(source.get("attachments"), list):
            return source["attachments"]
    return []


def infer_attachment_kind(item) -> str:
    if _is_record(item.get("image_item")):
        return "image"
    explicit = _read_string(item, "kind") or _read_string(item, "media_type") \
        or _read_string(item, "mediaType") or _read_string(item, "type_name")
    mime = _read_string(item, "mime_type") or _read_string(item, "mimeType")
    filename = _read_string(item, "filename") or _read_string(item, "file_name") \
        or _read_string(item, "name")
    normalized = f"{explicit} {mime} {filename}".lower()
    if any(word in normalized for word in ("image", "photo", "picture")) \
            or re.search(r"\.(svg|png|jpe?g|gif|webp|heic|bmp)$", filename):
        return "image"
    if "audio" in normalized or "voice" in normalized \
            or re.search(r"\.(mp3|wav|m4a|aac|ogg|flac|amr)$", filename):
        return "audio"
    if "video" in normalized or re.search(r"\.(mp4|mov|avi|mkv|webm)$", filename):
        return "video"
    return "file"


def _infer_mime_from_filename(filename: str) -> str:
    mapping = [
        (r"\.svg$", "image/svg+xml"), (r"\.png$", "image/png"),
        (r"\.jpe?g$", "image/jpeg"), (r"\.gif$", "image/gif"),
        (r"\.webp$", "image/webp"),
    ]
    for pattern, mime in mapping:
        if re.search(pattern, filename, re.IGNORECASE):
            return mime
    return ""


def _media_of(item):
    for key in ("image_item", "file_item", "video_item", "audio_item", "media_item"):
        if _is_record(item.get(key)):
            return item[key]
    return item


def _read_media_attachment(item, index: int):
    """item_list 中的媒体项解析。媒体标识兼容 file_id/media_id/encrypt_query_param，
    下载地址兼容 url/download_url/full_url——微信纯图片消息 image_item 只带 media 字段
    与 full_url，漏读会把图片静默丢掉。"""
    if _read_text_item(item):
        return None
    media = _media_of(item)
    media_payload = media.get("media") if _is_record(media.get("media")) else {}
    source = {**media, **media_payload}
    provider_file_id = (
        _read_number_or_string(source, "file_id") or _read_number_or_string(source, "fileId")
        or _read_number_or_string(source, "media_id") or _read_number_or_string(source, "mediaId")
        or _read_number_or_string(source, "id")
        or _read_number_or_string(source, "encrypt_query_param")
        or _read_number_or_string(source, "encryptQueryParam")
        or _read_number_or_string(source, "media")
        or _read_number_or_string(source, "md5")
    )
    download_url = (
        _read_string(source, "url") or _read_string(source, "download_url")
        or _read_string(source, "downloadUrl") or _read_string(source, "full_url")
        or _read_string(source, "fullUrl")
    )
    data_base64 = (
        _read_string(source, "data_base64") or _read_string(source, "dataBase64")
        or _read_string(source, "base64")
    )
    if not provider_file_id and not download_url and not data_base64:
        return None
    merged = {**item, **source}
    kind = infer_attachment_kind(merged)
    if kind not in SUPPORTED_ATTACHMENT_KINDS:
        return None
    filename = (
        _read_string(source, "filename") or _read_string(source, "file_name")
        or _read_string(source, "name")
        or (f"weixin-image-{index + 1}.jpg" if kind == "image" else f"weixin-attachment-{index + 1}")
    )
    mime = (
        _read_string(source, "mime_type") or _read_string(source, "mimeType")
        or _infer_mime_from_filename(filename)
        or {"image": "image/jpeg", "audio": "audio/mpeg", "video": "video/mp4"}.get(kind, "application/octet-stream")
    )
    size = (
        _read_number(source, "size") or _read_number(source, "sizeBytes")
        or _read_number(source, "file_size") or _read_number(source, "len")
        # image_item 没有通用 size 字段，mid_size 是实际图片大小（thumb_size 只是缩略图）
        or _read_number(source, "mid_size")
    )
    aes_key = (
        _read_string(source, "aes_key") or _read_string(source, "aesKey")
        or _read_string(source, "aeskey")
    )
    if kind in ("audio", "video") and not aes_key and not download_url:
        return None
    att = {
        "id": provider_file_id or download_url or f"weixin-{index + 1}",
        "kind": kind,
        "filename": filename,
        "mime_type": mime,
    }
    if size:
        att["size_bytes"] = size
    if provider_file_id:
        att["provider_file_id"] = provider_file_id
    if download_url:
        att["download_url"] = download_url
    if data_base64:
        att["data_base64"] = data_base64
    if aes_key:
        att["aes_key"] = aes_key
    return att


def _read_direct_attachment(item, index: int):
    """message.attachments 形态（管线重新包装的已解析附件）直接映射。"""
    if not _is_record(item):
        return None
    kind = _read_string(item, "kind")
    if kind not in SUPPORTED_ATTACHMENT_KINDS:
        return None
    att_id = _read_string(item, "id") or f"weixin-{index + 1}"
    filename = _read_string(item, "filename") or f"{att_id}.{kind}"
    mime = (
        _read_string(item, "mime_type") or _read_string(item, "mimeType")
        or {"image": "image/jpeg", "audio": "audio/mpeg", "video": "video/mp4"}.get(kind, "application/octet-stream")
    )
    size = _read_number(item, "size_bytes") or _read_number(item, "sizeBytes") or _read_number(item, "size")
    att = {"id": att_id, "kind": kind, "filename": filename, "mime_type": mime}
    if size:
        att["size_bytes"] = size
    for src, dst in (("providerFileId", "provider_file_id"), ("file_id", "provider_file_id"),
                     ("downloadUrl", "download_url"), ("download_url", "download_url"),
                     ("dataBase64", "data_base64"), ("data_base64", "data_base64"),
                     ("aesKey", "aes_key"), ("aes_key", "aes_key")):
        value = _read_string(item, src)
        if value and dst not in att:
            att[dst] = value
    return att


def read_weixin_text(message) -> str:
    text = (
        _read_string(message, "text") or _read_string(message, "content")
        or _read_string(message, "message")
    )
    if text:
        return text
    inner = _read_inner(message)
    items = _item_list_of(message)
    joined = "\n".join(t for t in (_read_text_item(i) for i in items) if t)
    if joined:
        return joined
    if inner:
        return _read_string(inner, "text") or _read_string(inner, "content")
    return ""


def read_weixin_attachments(message) -> list:
    attachments = []
    for i, item in enumerate(_item_list_of(message)):
        att = _read_media_attachment(item, i)
        if att:
            attachments.append(att)
    for i, item in enumerate(_direct_attachments_of(message)):
        att = _read_direct_attachment(item, i)
        if att:
            attachments.append(att)
    return attachments


def read_weixin_user_id(message) -> str:
    from_user = message.get("from") if _is_record(message.get("from")) else {}
    sender = message.get("sender") if _is_record(message.get("sender")) else {}
    inner = _read_inner(message)
    return (
        _read_string(message, "from_user_id") or _read_string(message, "from")
        or _read_string(message, "from_user") or _read_string(message, "fromUser")
        or _read_string(message, "user") or _read_string(message, "user_id")
        or _read_string(message, "userId")
        or _read_string(from_user, "id") or _read_string(from_user, "wxid")
        or _read_string(sender, "id") or _read_string(sender, "wxid")
        or (_read_string(inner, "from_user_id") if inner else "")
        or (_read_string(inner, "from") if inner else "")
        or (_read_string(inner, "user_id") if inner else "")
    )


def read_weixin_chat_id(message) -> str:
    for key in ("room", "room_id", "roomId", "chat", "chat_id", "chatId"):
        value = _read_string(message, key)
        if value:
            return value
    return ""


def read_weixin_display_name(message) -> str:
    from_user = message.get("from") if _is_record(message.get("from")) else {}
    sender = message.get("sender") if _is_record(message.get("sender")) else {}
    inner = _read_inner(message)
    return (
        _read_string(message, "name") or _read_string(message, "displayName")
        or _read_string(message, "nickname")
        or _read_string(from_user, "name") or _read_string(from_user, "nickname")
        or _read_string(sender, "name") or _read_string(sender, "nickname")
        or (_read_string(inner, "sender_name") if inner else "")
    )


def read_weixin_message_id(message) -> str:
    inner = _read_inner(message)
    for source in (message, inner or {}):
        for key in ("id", "msgid", "msgId", "msg_id", "message_id"):
            value = _read_string(source, key) or _read_number_or_string(source, key)
            if value:
                return value
    return ""


def read_weixin_context_token(message) -> str:
    inner = _read_inner(message)
    return (
        _read_string(message, "context_token") or _read_string(message, "contextToken")
        or _read_string(message, "context")
        or (_read_string(inner, "context_token") if inner else "")
    )


def parse_inbound(raw) -> dict | None:
    """原始消息 → 结构化入站。message_type==2（bot 自身）与空内容/缺发送者的消息返回 None。"""
    if _read_number(raw, "message_type") == WEIXIN_MESSAGE_TYPE_BOT:
        return None
    text = read_weixin_text(raw).strip()
    attachments = read_weixin_attachments(raw)
    user_id = read_weixin_user_id(raw).strip()
    if (not text and not attachments) or not user_id:
        return None
    chat_id = read_weixin_chat_id(raw)
    return {
        "text": text,
        "attachments": attachments,
        "user_id": user_id,
        "chat_id": chat_id,
        "is_group": bool(chat_id),
        "display_name": read_weixin_display_name(raw),
        "message_id": read_weixin_message_id(raw),
        "context_token": read_weixin_context_token(raw),
    }


def summarize_raw_message(raw, index: int) -> str:
    """只记录字段形状不记录正文——用于定位微信载荷形态变化导致的静默丢弃。"""
    inner = _read_inner(raw)
    items = _item_list_of(raw)
    item_types = ",".join(
        str(_read_number(i, "type") or _read_number(i, "item_type")
            or _read_number(i, "message_type") or "unknown")
        for i in items[:6] if _is_record(i)
    ) if items else "none"
    first_media = None
    for item in items:
        if _is_record(item):
            for key in ("image_item", "file_item", "video_item", "audio_item", "media_item"):
                if _is_record(item.get(key)):
                    first_media = item[key]
                    break
            if first_media:
                break
    media_keys = ",".join(list(first_media.keys())[:12]) if first_media else "none"
    nested = first_media.get("media") if first_media and _is_record(first_media.get("media")) else {}
    nested_keys = ",".join(list(nested.keys())[:8]) if nested else "none"
    return " ".join([
        f"index={index}",
        f"messageType={_read_number(raw, 'message_type') or 'none'}",
        f"textLen={len(read_weixin_text(raw).strip())}",
        f"attachments={len(read_weixin_attachments(raw))}",
        f"hasUserId={'yes' if read_weixin_user_id(raw).strip() else 'no'}",
        f"itemList={len(items)}",
        f"itemTypes={item_types}",
        f"keys={','.join(list(raw.keys())[:12])}",
        f"mediaKeys={media_keys}",
        f"nestedMediaKeys={nested_keys}",
        f"innerKeys={','.join(list(inner.keys())[:12]) if inner else 'none'}",
    ])


# ── 扫码注册（weixinRegistration.ts 全量语义）────────────────────


def normalize_qr_status(status) -> str:
    """二维码状态归一：数字 0/1/2/3|4 与多种字符串别名 → pending/scanned/success/expired/error。"""
    if isinstance(status, (int, float)) and not isinstance(status, bool):
        return {0: "pending", 1: "scanned", 2: "success"}.get(int(status), "expired" if int(status) in (3, 4) else "pending")
    if not isinstance(status, str):
        return "pending"
    lowered = status.lower()
    if lowered in ("confirmed", "confirm", "authorized", "success", "ok"):
        return "success"
    if lowered in ("scaned", "scanned", "scan", "confirmed_wait"):
        return "scanned"
    if lowered in ("expired", "timeout", "cancel", "cancelled", "canceled"):
        return "expired"
    if lowered in ("error", "failed", "fail"):
        return "error"
    return "pending"


def _registration_json(base_url: str, path: str):
    url = f"{base_url}{WEIXIN_BOT_API_PREFIX}{path}"
    try:
        resp = requests.get(url, headers={"iLink-App-ClientVersion": "1"},
                            timeout=WEIXIN_LOGIN_REQUEST_TIMEOUT_SEC)
    except requests.Timeout:
        raise  # 调用方区分 Timeout：扫码确认接口挂起不代表失败，返回 pending 继续轮询
    except requests.RequestException as e:
        raise WeixinApiError(f"Weixin login {path} transport error: {e}") from e
    if resp.status_code >= 300:
        raise WeixinApiError(f"Weixin login {path} failed: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise WeixinApiError(f"Weixin login {path} returned non-JSON body") from e
    # 扫码接口的 data 字段是平铺合并而非嵌套（ZCode unwrapData 语义）
    merged = dict(payload)
    if _is_record(payload) and _is_record(payload.get("data")):
        merged.update(payload["data"])
    ret = _read_number(merged, "ret")
    errcode = _read_number(merged, "errcode")
    if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
        raise WeixinApiError(
            _read_string(merged, "errmsg")
            or f"ret={'' if ret is None else ret} errcode={'' if errcode is None else errcode}".strip()
        )
    return merged


def begin_login(base_url: str = "") -> dict:
    """发起扫码登录。返回 {qrcode, qr_url, interval, expire_seconds}。
    qrcode 是轮询凭据，qr_url/qr_img 供渲染。"""
    base = (base_url or DEFAULT_WEIXIN_ILINK_BASE_URL).rstrip("/")
    payload = _registration_json(base, "/get_bot_qrcode?bot_type=3")
    qrcode = _read_string(payload, "qrcode") or _read_string(payload, "qr_code")
    qr_url = (
        _read_string(payload, "qrcode_img_content") or _read_string(payload, "qrcode_url")
        or qrcode
    )
    if not qrcode or not qr_url:
        raise WeixinApiError("Weixin login did not return a QR code.")
    return {
        "qrcode": qrcode,
        "qr_url": qr_url,
        "interval": WEIXIN_LOGIN_INTERVAL_SEC,
        "expire_seconds": _read_number(payload, "expires_in") or WEIXIN_LOGIN_EXPIRE_SEC,
    }


def poll_login(qrcode: str, base_url: str = "") -> dict:
    """轮询扫码状态。返回 {status, bot_token?, bot_id?}；
    接口超时视为 pending（服务端挂起等手机端确认是正常行为）。"""
    base = (base_url or DEFAULT_WEIXIN_ILINK_BASE_URL).rstrip("/")
    try:
        payload = _registration_json(base, f"/get_qrcode_status?qrcode={qrcode}")
    except requests.Timeout:
        return {"status": "pending"}
    status = normalize_qr_status(
        payload.get("status") or payload.get("qrcode_status") or payload.get("qr_status")
    )
    result = {"status": status}
    if status == "success":
        bot_token = _read_string(payload, "bot_token") or _read_string(payload, "token")
        if not bot_token:
            return {"status": "error", "message": "Weixin login succeeded but did not return bot_token."}
        result["bot_token"] = bot_token
        bot_id = _read_string(payload, "ilink_bot_id") or _read_string(payload, "bot_id")
        if bot_id:
            result["bot_id"] = bot_id
    elif status == "error":
        result["message"] = _read_string(payload, "errmsg") or "Weixin login failed."
    return result
