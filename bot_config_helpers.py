"""Bot 配置规范化与命令策略（照 ZCode config.ts + botConfigHelpers.ts 抄）。"""

from bot_config_constants import (
    DEFAULT_BOT_COMMANDS,
    DEFAULT_BOT_REPLY_GRANULARITY,
    ALL_BOT_WORKSPACES,
)

CREDENTIAL_PREFIX = "bot"


def create_default_bots_config():
    return {"version": 3, "bots": []}


def create_default_bot_commands():
    return dict(DEFAULT_BOT_COMMANDS)


def normalize_bot_command_policy(commands=None):
    """归一化命令策略。

    注意：和 ZCode 原文一样，历史 currentOptions.cli 字段不保留。
    """
    c = commands or {}
    policy = dict(DEFAULT_BOT_COMMANDS)
    for key in DEFAULT_BOT_COMMANDS:
        if key in c:
            policy[key] = c[key]
    return policy


def normalize_bot_current_options(options=None):
    """归一化当前选项。

    modelSelection 字段走 schema 校验（这里简化，假设上游已校验）。
    matorial 选项 model/thoughtLevel 只在导入时读取一次，保存时不保留。
    """
    o = options or {}
    normalized = {}
    if "modelSelection" in o:
        normalized["modelSelection"] = o["modelSelection"]
    for key in ("mode", "sandboxMode", "approvalPolicy"):
        if o.get(key):
            normalized[key] = o[key]
    return normalized


def normalize_bot_draft_options(options):
    """归一化草稿选项。"""
    normalized = {"provider": options.get("provider")}
    ms = options.get("modelSelection")
    if ms:
        normalized["modelSelection"] = ms
    if options.get("mode"):
        normalized["mode"] = options["mode"]
    return normalized


def get_default_bot_reply_granularity(provider=None):
    if not provider:
        return DEFAULT_BOT_REPLY_GRANULARITY
    # 飞书只支持 streaming_card，其他平台不支持
    if provider in ("feishu", "lark"):
        return "streaming_card"
    return DEFAULT_BOT_REPLY_GRANULARITY


def build_bot_credential_key(bot_id):
    return f"bot:{bot_id}:credential"


def build_bot_webhook_secret_key(bot_id):
    return f"bot:{bot_id}:webhook-secret"


def get_context_key(bot_id):
    return bot_id


def find_bot(config, bot_id):
    for bot in config.get("bots", []):
        if bot.get("id") == bot_id:
            return bot
    return None


def find_callback_bot(config, provider, payload=None):
    if not isinstance(payload, dict):
        return None
    bot_id = payload.get("botId", "") if isinstance(payload.get("botId"), str) else ""
    if bot_id:
        return find_bot(config, bot_id)
    for bot in config.get("bots", []):
        if bot.get("provider") == provider and bot.get("enabled"):
            return bot
    return None


def find_authorized_bot(config, actor):
    chat_type = actor.get("chatType")
    provider = actor.get("provider")
    user_id = actor.get("providerUserId")
    bot_id = actor.get("botId")

    if chat_type == "group" and provider in ("feishu", "lark"):
        for bot in config.get("bots", []):
            if (
                bot.get("enabled")
                and bot.get("id") == bot_id
                and bot.get("provider") == provider
            ):
                return bot
        return None

    if provider == "weixin":
        for bot in config.get("bots", []):
            if (
                bot.get("enabled")
                and bot.get("provider") == "weixin"
                and bot.get("id") == bot_id
            ):
                return bot
        return None

    for bot in config.get("bots", []):
        if (
            bot.get("enabled")
            and bot.get("provider") == provider
            and bot.get("providerUserId") == user_id
        ):
            return bot
    return None


def is_user_command_allowed(bot, requested_command):
    """判断命令是否允许（照 ZCode isUserCommandAllowed）。"""
    always_true = {"help", "message", "approve", "task", "stop"}
    if requested_command in always_true:
        return True
    policy = normalize_bot_command_policy(bot.get("allowedCommands", {}))
    if requested_command == "reconnect":
        return policy.get("workspace", True) is not False
    return policy.get(requested_command, True) is not False
