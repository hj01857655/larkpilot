import os
import shutil
from dotenv import load_dotenv

load_dotenv()

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]

CLAUDE_CLI = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-opus-4-6")
DEFAULT_CWD = os.path.expanduser(os.getenv("DEFAULT_CWD", "~"))
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "bypassPermissions")

SESSIONS_DIR = os.path.expanduser("~/.feishu-claude")

# 默认不因新消息腰斩长任务；后续消息排队，在当前 turn 结束后作为 follow-up 继续
INTERRUPT_ON_NEW_MESSAGE = os.getenv("INTERRUPT_ON_NEW_MESSAGE", "false").strip().lower() in (
    "1", "true", "yes", "on",
)

# 安静模式：干活时不刷工具进度/流式卡片，只在结果、失败、需要人回答时更新一次
QUIET_MODE = os.getenv("QUIET_MODE", "false").strip().lower() in (
    "1", "true", "yes", "on",
)

# 飞书单条消息长度硬上限；超出时截断而非让 transport 拒收
REPLY_MAX_CHARS = int(os.getenv("REPLY_MAX_CHARS", "1500"))

# 卡片按钮回调 HTTP 端口（需 ngrok 暴露）
CALLBACK_PORT = int(os.getenv("CALLBACK_PORT", "9981"))

# 回调服务绑定地址；默认只监听本机，避免未鉴权的 /handover 暴露到公网
CALLBACK_HOST = os.getenv("CALLBACK_HOST", "127.0.0.1")

# /handover 等本地控制接口的共享密钥；留空时这些接口直接禁用
CONTROL_TOKEN = os.getenv("CONTROL_TOKEN", "").strip()

# 群内命令门：auto=首个在群里执行命令的成员锁定为该群管理员（落盘），其余成员
# 的写命令（new/resume/model/mode/ws/cd）被拒；/stop 与 /help 始终放行（自救与入门）。
# off=全放行（向后兼容，单人自用群无风险）。
GROUP_ADMIN_ONLY = os.getenv("GROUP_ADMIN_ONLY", "auto").strip().lower()

# 流式卡片更新：每积累多少字符推送一次
STREAM_CHUNK_SIZE = int(os.getenv("STREAM_CHUNK_SIZE", "20"))

# ── 微信 iLink Bot 渠道 ──────────────────────────────────────────
# 无 token 时自动禁用；扫码注册：python weixin_login.py --save
WEIXIN_BOT_TOKEN = os.getenv("WEIXIN_BOT_TOKEN", "").strip()
WEIXIN_BOT_USER_ID = os.getenv("WEIXIN_BOT_USER_ID", "").strip()
WEIXIN_ENABLED = bool(WEIXIN_BOT_TOKEN)
WEIXIN_ILINK_BASE_URL = os.getenv("WEIXIN_ILINK_BASE_URL", "").strip()
# getupdates 长轮询服务端挂起时长（秒）；客户端超时要比它大
WEIXIN_GETUPDATES_TIMEOUT = int(os.getenv("WEIXIN_GETUPDATES_TIMEOUT", "90"))
# 游标持久化文件；游标在整批消息处理完成后才写入（官方 bugfix 语义）
WEIXIN_BUF_PATH = os.path.expanduser(
    os.getenv("WEIXIN_BUF_PATH", os.path.join(SESSIONS_DIR, "weixin_updates_buf.json"))
)
# 微信渠道回复长度上限（无卡片，纯文本分片）
WEIXIN_REPLY_MAX_CHARS = int(os.getenv("WEIXIN_REPLY_MAX_CHARS", "1500"))
