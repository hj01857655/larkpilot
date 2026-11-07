# 飞书开放平台 API 约束速查（关联本项目实现）

本文提炼本项目用到的飞书开放平台官方接口约束，每节标注本项目实现位置。数值与错误码以官方文档为准（来源 URL 见各节），整理日期 2026-11-07。

> 项目接入面总览：
> - 事件接收：WebSocket 长连接（`lark_oapi` SDK），入口 `main.py:1578` 注册三个 handler
> - 卡片回调：HTTP 回调服务 + ngrok 隧道（`main.py:1477` / `main.py:1483`），SDK 长连接回调与 Webhook 两条路径共入口 `on_card_action`（`main.py:1045`）
> - 消息发送/回复/卡片更新/表情回复/资源下载/群成员：`feishu_client.py` 封装

---

## 1. WebSocket 长连接（事件订阅方式）

来源：[使用长连接接收事件](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-/request-url-configuration-case)、[事件概述](https://open.feishu.cn/document/server-docs/event-subscription-guide/overview)

官方约束：

| 约束 | 值 |
|---|---|
| 适用应用类型 | 仅企业自建应用 |
| 事件处理时限 | **3 秒内处理完成且处理函数不抛异常**，否则视为推送失败触发重推 |
| 重推间隔 | 15 秒 → 5 分钟 → 1 小时 → 6 小时，最多 4 次 |
| 成功也可能重推 | 全链路 at-least-once，超时等异常均触发内部重发，**成功接收也会收到重复消息** |
| 连接数上限 | 每应用最多 50 个连接（每个 client 实例一个连接） |
| 多实例语义 | 集群模式，不广播：多 client 时**随机一个**收到消息 |
| 鉴权 | SDK 内置加密与鉴权，无需自行解密验签 |

**幂等是官方明确要求**（事件概述原文：「建议你在接收事件时对所有消息增加幂等逻辑」）。两个官方口径并存：

- 事件概述（通用）：2.0 版本事件用 `header.event_id` 判重
- 接收消息事件文档（特化）：**「如有幂等需求请使用 message_id 去重，不要依赖 event_id」**

本项目两者都做了，且均以业务键而非 event_id：

- 消息幂等：`seen_store.SeenStore`（`seen_store.py`），键为 `message_id`，TTL 600s / max 2000，落盘 `~/.feishu-claude/seen_messages.json`；入口 `main.py:87`、判重 `main.py:129` `_is_duplicate`
- 卡片回调幂等：`main.py:134` `_is_duplicate_callback`（触发者 + open_message_id + action），注释明言「飞书回调超时/非 2xx 会重推同一动作」

长连接初始化：`main.py:1583` `lark.ws.Client`，注册 `register_p2_im_message_receive_v1` / `register_p2_card_action_trigger` / `register_p2_im_chat_access_event_bot_p2p_chat_entered_v1`。

---

## 2. 接收消息事件 im.message.receive_v1

来源：[接收消息](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)

订阅前提：应用开启机器人能力，且在开发者后台订阅「消息与群组 → 接收消息 v2.0」。

权限矩阵（**不同消息范围是不同 scope，需分别申请**）：

| 收什么 | 需要的 scope（任一即可满足该行） |
|---|---|
| 用户发给机器人的单聊消息 | `im:message.p2p_msg` 或 `im:message.p2p_msg:readonly` |
| 群内用户/机器人 @ 机器人的消息（含机器人） | `im:message.group_at_msg.include_bot:readonly` |
| 群内仅用户 @ 机器人的消息 | `im:message.group_at_msg` 或 `im:message.group_at_msg:readonly` |
| 群内所有消息（含其他机器人） | `im:message.group_msg.include_bot:read` |
| 群内所有用户消息（不含机器人） | `im:message.group_msg`（敏感权限） |

关键字段语义：

- `event.sender.sender_id.{open_id,union_id,user_id}`；`user_id` 需额外权限 `contact:user.employee_id:readonly`，本项目只依赖 `open_id`
- `event.sender.sender_type`：`user` / `bot`（区分真人消息与机器人消息）
- `event.message.chat_type`：`p2p` / `group`
- `event.message.mentions[].key`：`@_user_N`（N 从 1 起），`mentions[].id` 是 ID 对象（`open_id` 等）
- `user_agent` 字段需独立权限 `im:user_agent:read`

本项目接入：

- 入口 `on_message_receive`（`main.py:1269`）；`extract_chat_info`（`main.py:380`）把 p2p 的 `chat_id` 归一为 `sender_id.open_id`（`chat_id == user_id`），群的 `chat_id` 原样保留——全树「会话归属键」由此处定形
- 群文本结构化解析（mentions 提取、@ 判定）：`main.py:201` `_parse_group_text`、`main.py:241` `_is_addressed`
- bot 发进私聊触发的事件：`on_bot_p2p_chat_entered`（`main.py:1276`）

---

## 3. 发送消息 create

来源：[发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)

`POST /open-apis/im/v1/messages?receive_id_type=...`

| 约束 | 值 |
|---|---|
| 用户限频 | 同一用户 5 QPS |
| 群限频 | 同一群组内**所有机器人共享** 5 QPS |
| 接口级限频 | 1000 次/分钟、50 次/秒 |
| 文本消息体 | ≤ 150 KB |
| 卡片/富文本消息体 | ≤ 30 KB（模板卡片按模板展开后计） |
| `uuid` 去重 | 相同 uuid 1 小时内至多成功一条，≤ 50 字符 |
| `receive_id_type` | `open_id` / `union_id` / `user_id` / `email` / `chat_id` |
| `msg_type` | text / post / image / file / audio / media / sticker / interactive / share_chat / share_user / system |
| 权限 | `im:message` / `im:message:send_as_bot` / `im:message:send` 任一 |

mentions 语法：文本内 `@_user_N` 占位，请求体 `mentions` 数组 `{"key":"@_user_1","id":{"open_id":"ou_xxx"}}`；卡片 markdown 用 `<at id="ou_xxx"></at>`。

本项目接入：`send_text_to_user`（`feishu_client.py:532`，`receive_id_type="open_id"`）、`send_card_to_user`（`feishu_client.py:279`）。文本分片规避 230025：`split_long_reply_text`（见第 5 节引用的调用方）。

关键错误码：230001 参数错 / 230002 机器人不在群 / 230006 未开机器人能力 / 230013 可用范围外 / 230020 限频 / 230022 敏感信息 / 230025 超长 / 230027 缺权限 / 230034 receive_id 无效 / 230035 发送权限拒绝 / 230053 用户屏蔽机器人 / 230099 卡片构建失败。

---

## 4. 回复消息 reply

来源：[回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply)

`POST /open-apis/im/v1/messages/:message_id/reply`

- 限频与消息体上限同 create（同一用户 5 QPS、群内机器人共享 5 QPS；文本 150 KB / 卡片富文本 30 KB）
- `reply_in_thread`：以话题形式回复；目标已是话题消息则强制按话题
- `uuid` 幂等语义同 create
- 特有错误码：230011 消息已撤回 / 230050 消息不可见 / 230071 群不支持话题回复 / 230072 聚合消息不支持话题回复

本项目接入：`reply_text`（`feishu_client.py:506`，走 `im.v1.message.areply`，经 `_retry_with_backoff` 重试，`feishu_client.py:209`）、`reply_card`（`feishu_client.py:306`）。

---

## 5. 更新已发送的消息卡片 patch（与「编辑消息」区分）

来源：[更新已发送的消息卡片](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch)

`PATCH /open-apis/im/v1/messages/:message_id`（只更新 `interactive` 类型）

| 约束 | 值 |
|---|---|
| 卡片消息体 | ≤ 30 KB |
| 有效期 | 仅支持更新 **14 天内** 发送的消息（230031） |
| 单卡限频 | 5 QPS |
| 共享卡片 | 更新前后卡片的 `config.update_multi` 均须显式 `true`（对所有接收者可见） |
| 身份一致 | 调用更新的 access_token 必须与发送卡片的身份一致 |
| 权限 | `im:message` / `im:message:send_as_bot` / `im:message:update` 任一 |

注意区分：PUT 同路径是[编辑消息](https://open.feishu.cn/document/server-docs/im-v1/message/update)（编辑文本/富文本，一条消息最多编辑 20 次，仅可编辑自己发的消息），不支持卡片。

本项目接入：`update_card`（`feishu_client.py:332`）、带按钮变体 `update_card_with_buttons`（`feishu_client.py:441`）、`update_card_elements`（`feishu_client.py:482`）——任务流式刷新回复卡片即走此面。

关键错误码：230011 已撤回 / 230031 超 14 天 / 230110 已删除 / 230099 卡片构建失败（子错误码 100290 卡片内无效人员 id、200621 卡片 JSON 解析失败等）。

---

## 6. 卡片回传交互回调 card.action.trigger

来源：[卡片回传交互回调](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-callback-communication)

| 约束 | 值 |
|---|---|
| 响应时限 | **3 秒内响应**，否则客户端报 200341「未在规定时间内响应」 |
| 响应状态 | 必须非 3xx（HTTP 3xx 会导致交互请求错误）、非 200 报 200671 |
| 重推 | 超时/非 2xx 会重推同一动作（与事件重推机制同源，官方未给 card 回调独立次数表） |
| 回调凭证 | `event.token`：延时更新卡片用的凭证，**有效期 30 分钟、最多可更新 2 次** |
| 权限 | 无需额外 scope；`operator.user_id` 属敏感字段（本项目只用 `operator.open_id`） |

回调体关键字段：

- `event.operator.open_id`：点击者（本项目授权门与归属键的依据）
- `event.action.value`：按钮绑定的自定义回传数据（string 或 object）
- `event.context.open_message_id` / `open_chat_id`：卡片所在消息/会话
- `event.action.tag`：组件标签（button 等）；表单容器数据在 `event.action.form_value`

响应体（同步应答）：`toast`（`type`: info/success/error/warning + `content`/`i18n`）和/或 `card`（`type: raw` 卡片 JSON / `template` 模板）——**响应里直接带新卡片即可完成「点击后更新卡片」，不必再调 PATCH**。

本项目接入：

- 共入口 `on_card_action`（`main.py:1045`）：`event.operator.open_id` → `admission.is_allowed` 授权门 → 回调幂等 → 群命令门（`main.py:1081`）；拒绝路径用 `CallBackToast`（type=error）同步拒绝。文本路径命令经 `split_long_reply_text`（`reply_formatter.py:136`）
- HTTP 通道 `_CardCallbackHandler`（`main.py:1324`）：处理 URL 校验 challenge，响应 `{"toast": {...}}` 同步应答；ngrok 隧道 `main.py:1483`

---

## 7. 添加消息表情回复 reaction

来源：[添加消息表情回复](https://open.feishu.cn/document/server-docs/im-v1/message-reaction/create)

`POST /open-apis/im/v1/messages/:message_id/reactions`，请求体 `{"reaction_type": {"emoji_type": "SMILE"}}`

| 约束 | 值 |
|---|---|
| 权限 | `im:message` 或 `im:message.reactions:write_only` |
| emoji_type | 必须是[表情文案说明](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message-reaction/emojis-introduce)中的合法值（非法报 231001） |
| 限制 | 已撤回消息、system 类型消息不可加表情 |
| 返回 | `reaction_id`——删除表情（DELETE 同路径 `/reactions/:reaction_id`）必须用它 |

关键错误码：231001 emoji 非法 / 231002 操作者不在会话内 / 231003 消息不存在或已撤回 / 231015 重复请求处理中。

本项目接入：状态表情机制 `feishu_client.py:561` 的 `_STATUS_EMOJI`（OneSecond=排队 / OnIt=执行中 / CheckMark=完成 / CrossMark=失败）——飞书无原生 typing 状态，用表情标记任务进度；`_add_reaction`（`feishu_client.py:568`，失败不抛，非关键路径）、`_delete_reaction`（`feishu_client.py:599`）；reaction_id 落盘与启动清孤儿 `cleanup_orphan_reactions`（`feishu_client.py:144`，映射落盘 `_load_reactions`/`_flush_reactions`）。

---

## 8. 获取消息中的资源文件 resources

来源：[获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)

`GET /open-apis/im/v1/messages/:message_id/resources/:file_key?type=image|file`，返回二进制流（类型看响应头 `Content-Type`）

| 约束 | 值 |
|---|---|
| 权限 | `im:message` / `im:message:readonly` / `im:message.history:readonly` 任一 |
| `type=image` | 不支持 Range 分片，仅完整下载 < 100 MB |
| `type=file` | 未分片仅 < 100 MB；≥ 100 MB 须 `Range: bytes=<start>-<end>` 分片（单片 ≤ 32 MB，错误码 234046） |
| 匹配性 | `file_key` 与 `message_id` 必须匹配（234003） |
| 限制 | 不支持表情包资源；不支持合并转发消息、消息卡片内的资源（234043）；保密/防泄密消息不可下载（234038） |
| 前提 | 机器人与消息须在同一会话（234004） |

本项目接入：`download_image`（`feishu_client.py:356`）、`download_file`（`feishu_client.py:362`）、同步下载体 `_download_resource_sync`（`feishu_client.py:371`，边读边限长防 Content-Length 造假）；启动清理过期附件 `_cleanup_stale_downloads`（`main.py:1520`，`%TEMP%/feishu-*` 超 24h 删——排队的 run 可能数小时后才消费附件，故不即删）。

---

## 9. 获取群成员列表 members

来源：[获取群成员列表](https://open.feishu.cn/document/server-docs/group/chat-member/get)

`GET /open-apis/im/v1/chats/:chat_id/members?member_id_type=open_id&page_size=20&page_token=...`

| 约束 | 值 |
|---|---|
| 权限 | `im:chat` / `im:chat:readonly` / `im:chat.members:read` 任一 |
| 分页 | `page_size` 默认 20、**最大 100**；`has_more` + `page_token` 翻页；`member_total` 总数 |
| 前提 | 操作者（机器人）必须在被查群内（232011）；内部群须同租户（232010） |
| 返回偏差 | **不返回机器人成员**（可能少于 page_size）；同一时间入群的成员一次性返回（可能多于 page_size） |
| `member_id_type` | `open_id`（默认）/ `union_id` / `user_id`；`user_id` 需 `contact:user.employee_id:readonly` |

本项目接入：`_chat_members_page_sync`（`feishu_client.py:410`）、翻页生成器 `chat_members_page_reader`（`feishu_client.py:432`）、合并进群成员目录 `_merge_member_directory`（`main.py:232`）——用于群内 `@人名` 的 MentionIndex 解析（`main.py:123`）。

---

## 10. 获取机器人信息 bot/v3/info

来源：[获取机器人信息](https://open.feishu.cn/document/client-docs/bot-v3/obtain-bot-info)

`GET /open-apis/bot/v3/info`

- 无需申请权限 scope，仅需 `tenant_access_token` + 已启用机器人能力
- 返回 `bot.open_id`（机器人自身的 open_id）、`bot.app_name`、`bot.avatar_url`、`bot.activate_status`（2=租户启用）、`bot.ip_white_list`

本项目接入：`_fetch_bot_open_id_sync`（`feishu_client.py:174`）→ `get_bot_open_id`（`feishu_client.py:187`）——区分「@ 的是自己」与「别人」，供 `_is_addressed`（`main.py:241`）判定群内发言是否点名本 bot。

---

## 11. 通用约束

- **限频总纲**：本项目用到的全部读/写接口共用接口级 1000 次/分钟、50 次/秒；消息类另有「同一用户 5 QPS、群内机器人共享 5 QPS」。项目内发送限流在 feishu_client 的发送路径统一管控。
- **token**：服务端接口一律 `tenant_access_token`（`Bearer t-...`），卡片 PATCH 要求与发卡身份一致（第 5 节）。
- **幂等三处**：事件用 message_id（第 1、2 节官方口径）、uuid（create/reply 发送侧）、卡片回调（第 6 节重推）。项目对应 `seen_store.py`、`main.py:129`、`main.py:134`。
- **错误码命名域**：`2300xx` 消息收发、`2310xx` 表情回复、`2320xx` 群管理、`2340xx` 消息资源、`200xxx` 卡片交互（客户端侧）。通用错误码见[服务端通用错误码](https://open.feishu.cn/document/ukTMukTMukTM/ugjM14COyUjL4ITN)。
