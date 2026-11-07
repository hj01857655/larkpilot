# -*- coding: utf-8 -*-
"""mention_resolver 语义测试（标准 pytest 风格，UTF-8 可读）。

覆盖：精确匹配、前缀截断、首词拆分、大小写归一化、歧义不猜、
未命中保持原文、@_user_1 占位符不误伤、名字延伸不拆、优先精确。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mention_resolver import MentionIndex, resolve_mentions, candidate_ref


@pytest.fixture
def idx():
    index = MentionIndex()
    index.add("张三", "ou_zhangsan")
    index.add("李四", "ou_lisi")
    index.add("王五", "ou_wangwu")
    index.add("John Smith", "ou_john")
    index.add("zhang-san", "ou_zhangsan_ascii")
    index.add("ZhangSan", "ou_zhangsan_camel")
    return index


# ── 精确匹配 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expect",
    [
        ("@张三 在吗", "ou_zhangsan"),
        ("@李四 也在", "ou_lisi"),
        ("@John Smith 有点长", "ou_john"),
    ],
)
def test_exact_match(idx, text, expect):
    out, _ = resolve_mentions(text, idx)
    assert expect in out


# ── 前缀截断（token 贪婪下拆出前缀名字）──────────────────


def test_prefix_trunc_chinese(idx):
    """@张三可以帮忙 → 消费"张三"，尾巴"可以帮忙"接在后面"""
    out, _ = resolve_mentions("你好，@张三可以帮忙", idx)
    assert "ou_zhangsan" in out
    assert "可以帮忙" in out


def test_prefix_trunc_with_punct(idx):
    out, _ = resolve_mentions("提到 @John，他在吗", idx)
    assert "ou_john" in out
    assert "，他在吗" in out


# ── 首词拆分（英文 token 贪婪）────────────────────────


def test_first_word_split(idx):
    out, _ = resolve_mentions("ask @John for help", idx)
    assert "ou_john" in out
    assert "for help" in out


def test_ascii_name_first_word_split(idx):
    out, _ = resolve_mentions("@zhang-san is cool", idx)
    assert "ou_zhangsan_ascii" in out
    assert "is cool" in out


# ── 大小写/词形归一化 ────────────────────────────────


def test_uppercase_cjk_ascii(idx):
    """ZhangSan 是独立书写形式，与 zhang-san 不同条目，各自匹配"""
    out, _ = resolve_mentions("@ZhangSan 你好", idx)
    assert "ou_zhangsan_camel" in out


def test_hyphen_preserved_not_collapsed(idx):
    """zhang-san 和 zhang-san 精确匹配 (不剥连字符对齐 ZCode)"""
    out, _ = resolve_mentions("@zhang-san 你好", idx)
    assert "ou_zhangsan_ascii" in out


def test_space_variant_does_not_collide(idx):
    """"zhang san" 与 "zhang-san" 不同书写形式，不碰撞 -> 保留原文或按各自规则"""
    out, _ = resolve_mentions("@zhang san 你好", idx)
    assert out == "@zhang san 你好"  # 未在目录中精确登记，保持原文


def test_john_smith_across_spellings(idx):
    """@johnsmith 是独立 entry，与 "John Smith" (带空格) 不混淆"""
    out, _ = resolve_mentions("@john smith 看一下", idx)
    assert "ou_john" in out  # 首词 "john" 匹配 John Smith entry


# ── 歧义 / 不猜 ──────────────────────────────────────


def test_names_extension_not_broken(idx):
    """张三丰 是名字延伸，不是张三——不拆"""
    out, _ = resolve_mentions("@张三丰 hello", idx)
    assert out == "@张三丰 hello"


def test_smith_suffix_not_broken(idx):
    """@smith 不是 John Smith 的头——不猜"""
    out, _ = resolve_mentions("@smith hello", idx)
    assert out == "@smith hello"


def test_ambiguous_name_not_guessed():
    idx2 = MentionIndex()
    idx2.add("张三", "ou_a")
    idx2.add("张三", "ou_b")
    out, _ = resolve_mentions("@张三 来一下", idx2)
    assert out == "@张三 来一下"


# ── 未命中保持原文 ───────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["@all 通知", "@_user_1 占位", "@孤独 没有", "@123 @数字不会进"],
)
def test_miss_preserve_original(idx, text):
    out, _ = resolve_mentions(text, idx)
    assert out == text


# ── 精确优先（多 entry 前缀重叠）──────────────────────


def test_prefix_precise_wins():
    idx3 = MentionIndex()
    idx3.add("张三丰", "ou_zhangsf")
    idx3.add("张三", "ou_zhangsan")
    out, _ = resolve_mentions("@张三 在吗", idx3)
    assert "ou_zhangsan" in out
    assert "ou_zhangsf" not in out

    out2, _ = resolve_mentions("@张三丰 在吗", idx3)
    assert "ou_zhangsf" in out2


# ── 混合文本多 @ 替换 ──────────────────────────────────


def test_multiple_mentions(idx):
    out, _ = resolve_mentions("@张三 和 @李四 都来了，@all 没通知", idx)
    assert "ou_zhangsan" in out
    assert "ou_lisi" in out
