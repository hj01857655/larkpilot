"""
微信 bot 扫码注册 CLI。

用法：
    python weixin_login.py              # 终端展示二维码（文字块）+ 轮询
    python weixin_login.py --save       # 成功后把 token/bot_id 写入 .env
    python weixin_login.py --qr-out x.png / x.html

流程：GET /get_bot_qrcode?bot_type=3 拿 qrcode → 3s 轮询 /get_qrcode_status
→ success 时输出 bot_token + ilink_bot_id。状态接口超时视为 pending 继续轮。
"""

import argparse
import base64
import os
import sys
import time

import requests

import weixin_client as wx


def save_qr(qr_url: str, out_path: str):
    if qr_url.startswith("data:image"):
        payload = qr_url.split(",", 1)[1] if "," in qr_url else ""
        data = base64.b64decode(payload)
        with open(out_path, "wb") as f:
            f.write(data)
    elif qr_url.startswith(("http://", "https://")):
        resp = requests.get(qr_url, timeout=30)
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(resp.content)
    else:
        # 纯文本形态（可能是待渲染的 qr 内容），原样落盘
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(qr_url)
    print(f"QR code saved to: {os.path.abspath(out_path)}")


def render_ascii_qr(qr_url: str):
    try:
        import qrcode  # noqa: PLC0415 可选依赖
    except ImportError:
        print("(未安装 qrcode 库，跳过终端二维码；请用 --qr-out 保存图片后扫码)")
        return
    data = qr_url if qr_url.startswith(("http://", "https://")) else qr_url.split(",", 1)[-1]
    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.print_ascii(invert=True)


def write_env(token: str, bot_id: str, env_path: str = ".env"):
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    updates = {"WEIXIN_BOT_TOKEN": token}
    if bot_id:
        updates["WEIXIN_BOT_USER_ID"] = bot_id
    for key, value in updates.items():
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f".env updated: WEIXIN_BOT_TOKEN, WEIXIN_BOT_USER_ID")


def main():
    parser = argparse.ArgumentParser(description="Weixin iLink bot QR login")
    parser.add_argument("--save", action="store_true", help="write token/bot_id into .env")
    parser.add_argument("--qr-out", default="", help="save QR image to this path")
    parser.add_argument("--base-url", default="", help="override iLink base URL")
    args = parser.parse_args()

    login = wx.begin_login(args.base_url)
    print(f"QR code obtained, expires in ~{login['expire_seconds']}s. Scan with Weixin.")
    render_ascii_qr(login["qr_url"])
    if args.qr_out:
        save_qr(login["qr_url"], args.qr_out)

    deadline = time.time() + max(login["expire_seconds"], 60) + 30
    last_status = ""
    while time.time() < deadline:
        result = wx.poll_login(login["qrcode"], args.base_url)
        status = result["status"]
        if status != last_status:
            print(f"[{time.strftime('%H:%M:%S')}] status: {status}")
            last_status = status
        if status == "success":
            print("Login OK.")
            print(f"bot_token: {result['bot_token'][:8]}...{result['bot_token'][-4:]}")
            if result.get("bot_id"):
                print(f"bot_id (ilink_bot_id): {result['bot_id']}")
            if args.save:
                write_env(result["bot_token"], result.get("bot_id", ""))
            else:
                print("(未加 --save，请手动写入 .env：WEIXIN_BOT_TOKEN / WEIXIN_BOT_USER_ID)")
            return 0
        if status == "error":
            print(f"Login failed: {result.get('message', 'unknown')}")
            return 1
        if status == "expired":
            print("QR code expired, restart to get a new one.")
            return 1
        time.sleep(login["interval"])
    print("Timed out waiting for scan.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
