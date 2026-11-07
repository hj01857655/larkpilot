"""命令顺序常量（照 ZCode commandOrder.ts 抄）。

菜单顺序只用于展示，授权判定走 allowedCommands 策略不动。
"""

BOT_POLICY_COMMAND_ORDER = ["status", "new", "workspace", "model", "mode", "thoughtLevel", "reply"]
BOT_MENU_COMMAND_ORDER = ["help"] + BOT_POLICY_COMMAND_ORDER + ["bind"]
