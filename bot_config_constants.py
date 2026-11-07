"""Bot 配置共享常量（照 ZCode shared/bots.ts 抄）。"""

# 通配符 workspace 值。空数组归一到 "*"，表示不限制
ALL_BOT_WORKSPACES = "*"

# 绑定码有效期 30 秒
BOT_BIND_CODE_TTL_MS = 30_000

# 默认命令策略：全部为 True
DEFAULT_BOT_COMMANDS = {
    "status": True,
    "new": True,
    "workspace": True,
    "model": True,
    "mode": True,
    "thoughtLevel": True,
    "reply": True,
}

# 默认回复颗粒度
DEFAULT_BOT_REPLY_GRANULARITY = "assistant_changes"
