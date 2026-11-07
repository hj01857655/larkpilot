"""
群 @ 提及名称 → open_id 解析器（纯文本版）。

照 ZCode channelMentionResolver.ts 的机制抄（对齐 no-guess 铁律）：
- 名字只是查询条件，候选身份只能来自本地目录（消息事件 mention 节点）
- NFKC + lower + 移除空白/连字符 归一化比较
- 消歧漏斗（对齐 ZCode lines 65-69）：
  * 精确唯一 → 替换
  * 不精确但唯一候选 → 也替换（单候选兜底）
  * 多候选 → 歧义：保留原文，不猜
- 零候选 → 保持原文（未命中不是错误）

文本版与结构化 part 的三个差异点：
1. 文本 token 贪婪："ask @John for help" 整个进 token → ASCII 首词拆分，
   首词 @John 反查 John Smith（方向 B：token 是 entry 名的头部）
2. 中文名后接句子："@张三可以帮忙"——entry "张三" 是 token 前缀
   （方向 A），剩余 "可以帮忙" 呈句子接续特征才允许截断替换；
   剩余像名字延伸（"@张三丰" 里的 "丰"）时不拆、不猜
3. 精确优先覆盖前缀重叠："@张三" 与目录中 "张三丰"、"张三" 并存时
   精确命中 "张三"，而不是歧义
"""

import hashlib
import re
import unicodedata

# 对齐 ZCode MAX_CANDIDATES=10
MAX_CANDIDATES = 10

_MENTION_RE = re.compile(
    r"@([A-Za-z][A-Za-z0-9 _\-]*|[一-鿿][一-鿿 _\-]*)(?![A-Za-z0-9_])"
)

# 中文句子接续词首字符："可以帮忙"、"来处理"、"去开会"
_SENTENCE_LEADERS = set(
    "可帮要能请让给看来去说的好们的吗吧呢哦嗯上下中里也都就"
    "没把被对从往支加使这是那"
)
_PUNCT_OR_SPACE = set(" \t,。!?；:：、”，、；!")


def _norm(text: str) -> str:
    """
    NFKC + trim + lower — 对齐 ZCode normalizeName 原文，不剥空格/连字符。

    原因：剥了空格后 "John Smith" 和 "JohnSmith" 归一到同一 key，
    两个真实不同的人被挤成假歧义。ZCode 明文规则就是 NFKC + trim + lower。
    """
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text).strip().lower()


def _rest_looks_like_sentence(rest: str) -> bool:
    """逐个原文字符判定——净掉开头空白/标点后看首个实义字符。

    "可以帮忙" → 可 是句子接续词 → True
    "丰不存在" → 丰 不是接续词（姓+名延伸）→ False
    " for help" → 空白 → True（ASCII 句子开头）
    "" → True（恰好完整命中）
    """
    rest = rest.lstrip()
    if not rest:
        return True
    r0 = rest[0]
    if r0 in _PUNCT_OR_SPACE:
        return True
    if "一" <= r0 <= "鿿":
        return r0 in _SENTENCE_LEADERS
    # ASCII 续词（"for help"）：剩余里出现空白才算句子
    return any(c.isspace() for c in rest[:8])


class MentionIndex:
    """name→open_id 本地目录。

    数据源：消息事件里的 msg.mentions 节点，每条同时带 name 和 open_id。
    同名重复 ID 保留为独立候选（歧义源不猜）。
    """

    def __init__(self):
        self._entries: list[tuple[str, str]] = []  # (norm_name, open_id)

    def add(self, name: str, open_id: str) -> None:
        if name and open_id:
            self._entries.append((_norm(name), open_id))

    def add_mentions(self, mentions: list) -> None:
        """从飞书 mention 节点列表累积。

        SDK Mention 节点：key(占位符 @_user_1)、id(open_id-type 时是
        ou_xxx)、id_type、name(展示名)。为防 mock 测试对象的差异，
        兼容 open_id 属性名。
        """
        for m in mentions or []:
            name = getattr(m, "name", "") or ""
            oid = (
                getattr(m, "open_id", "")
                or getattr(m, "id", "")
                or ""
            )
            if name and oid and getattr(m, "id_type", "open_id") == "open_id":
                self.add(name, oid)

    def add_directory(self, members: dict[str, str]) -> None:
        """合并群成员目录（{open_id: name}）。

        消息事件 mention 节点只在被 @ 时出现；目录把没被 @ 过的成员也
        纳入候选。同名重复 ID 保留为独立候选——歧义源不猜，与 ZCode 一致。
        """
        for oid, name in (members or {}).items():
            self.add(name, oid)

    # ── 查询 ─────────────────────────────────────────────

    def resolve_token(self, raw: str) -> tuple[str | None, int, bool]:
        """解析单个 @token。

        返回 (open_id 或 None, 消费的 raw 字符数, 是否歧义)。
        歧义 = 有候选但无法确定唯一 ID——调用方应保留原文并提示。

        规则优先级（对齐 ZCode lines 65-69）：
        1. 精确：token 首词 norm 后等于某 entry norm 名 → 替换首词
        2. 前缀（方向 A）：entry norm 名是首词 norm 的前缀，唯一候选，
           且原文尾巴呈句子接续特征 → 替换，消费 entry 名对应的字符数
        3. 反向（方向 B）：首词 norm 是某 entry norm 名的前缀，唯一候选
           → 替换首词（@john → John Smith）
        4. 多候选 → 歧义不猜；名字延伸但非句子 → 歧义不猜
        """
        # 原文首词（空格切分，保留词后原文做尾巴判句）
        parts = raw.strip().split()
        if not parts:
            return None, 0, False
        word = parts[0]
        word_n = _norm(word)

        # 1. 精确匹配
        exact = [oid for nu, oid in self._entries if nu == word_n]
        if exact:
            uids = {oid for oid in exact}
            if len(uids) == 1:
                oid = exact[0] if exact[0] in uids else next(iter(uids))
                return oid, len(word), False

        # 2. 前缀（方向 A）
        pre = [(nu, oid) for nu, oid in self._entries if nu and word_n.startswith(nu)]
        pids = {oid for _, oid in pre}
        if pids and len(pids) == 1:
            nu, oid = next(p for p in pre if p[1] in pids)
            # 归一化前缀对应的原文长度（跳过空格/连字符）
            consumed = len(word)
            acc: list[str] = []
            for i, ch in enumerate(word):
                if ch in (" ", "-"):
                    continue
                acc.append(ch)
                if _norm("".join(acc)) == nu:
                    consumed = i + 1
                    break
            rest = raw[consumed:]
            if _rest_looks_like_sentence(rest):
                return oid, consumed, False
            # 前缀名字有延伸但不是句子——歧义（张三不是张三丰）
            return None, 0, True

        # 3. 反向（方向 B）
        suf = [(nu, oid) for nu, oid in self._entries if nu.startswith(word_n)]
        suids = {oid for _, oid in suf}
        if suids and len(suids) == 1:
            oid = next(iter(suids))
            return oid, len(word), False

        # 4. 有候选但不唯一——歧义
        if pre or suf:
            return None, 0, True
        return None, 0, False


def resolve_mentions(text: str, index: MentionIndex) -> tuple[str, list[str]]:
    """替换文本中的 @name 为飞书 <at id=...> 标签。

    返回 (替换文本, 歧义名称列表)。
    """
    ambiguous: list[str] = []

    out: list[str] = []
    last = 0
    for m in _MENTION_RE.finditer(text):
        raw = m.group(1).strip()
        out.append(text[last:m.start()])  # @ 之前的文本（不含 @）
        oid, consumed, is_ambiguous = index.resolve_token(raw)
        if oid:
            out.append(f"<at id={oid}></at>")
            if consumed < len(raw):
                out.append(raw[consumed:])  # token 留下的尾巴（句子续词）
        elif is_ambiguous:
            if raw not in ambiguous:
                ambiguous.append(raw)
            out.append(m.group(0))  # 歧义：保留原文不猜
        else:
            out.append(m.group(0))  # 未命中：保留原文
        last = m.end()
    if last < len(text):
        out.append(text[last:])
    return "".join(out), ambiguous


def candidate_ref(scope: str, target_id: str) -> str:
    """稳定候选 ref key（对齐 ZCode name:sha256(scope, targetId))。"""
    payload = hashlib.sha256(f"{scope}|{target_id}".encode("utf-8")).hexdigest()
    return f"name:{payload}"
