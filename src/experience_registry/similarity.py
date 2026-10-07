"""术语归一化与相似条目发现。

不同班组对同一事物使用不同术语（如"窜动"/"轴向位移"）。管理员维护同义词组，
归一化后再做字符二元组 Jaccard 相似度，整个过程确定、可解释，不依赖外部模型。
"""
from __future__ import annotations

import re
from .models import VersionContent

_WORD_RE = re.compile(r"[a-z0-9]+")
# 领域内常见同义词默认组：同一组内的词归一到该组排序后的第一个写法
DEFAULT_SYNONYM_GROUPS: list[list[str]] = [
    ["轴向位移", "窜动", "轴窜", "主轴窜动"],
    ["振动超标", "震动大", "振动大", "振动异常"],
    ["温升过高", "发热", "发烫", "温度高"],
    ["液压泵", "油泵"],
    ["密封圈", "油封", "o型圈"],
    ["联轴器", "对轮"],
    ["皮带轮", "带轮"],
    ["进给", "走刀"],
    ["主轴箱", "床头箱"],
    ["导轨", "滑轨"],
]

STOPWORDS = {
    "设备", "故障", "异常", "情况", "进行", "出现", "导致", "需要", "可以",
    "如果", "when", "the", "and", "for", "with",
}


def canonical_synonym_map(groups: list[list[str]]) -> dict[str, str]:
    """把同义词组转成 词 -> 规范词 的映射，规范词取组内 Unicode 排序最小者。

    排序使映射结果与录入顺序无关，保证确定性。
    """
    mapping: dict[str, str] = {}
    for group in groups:
        words = sorted({w.strip().casefold() for w in group if w.strip()})
        if not words:
            continue
        canonical = words[0]
        for word in words:
            mapping[word] = canonical
    return mapping


def normalize_text(text: str, synonyms: dict[str, str]) -> str:
    folded = text.casefold()
    if not synonyms:
        return folded
    # 单次扫描替换（长词优先），避免短词在已替换文本上级联替换
    alt = "|".join(sorted((re.escape(w) for w in synonyms),
                          key=len, reverse=True))
    return re.sub(alt, lambda m: synonyms[m.group()], folded)


def _char_bigrams(text: str) -> set[str]:
    # 中文字符按二元组切分；连续 ASCII/数字按整词
    tokens: set[str] = set()
    chunks: list[str] = []
    buf = ""
    for ch in text:
        if ch.isascii() and (ch.isalnum()):
            buf += ch
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if "一" <= ch <= "鿿":
            chunks.append(ch)
    if buf:
        chunks.append(buf)
    # 英文词
    for m in _WORD_RE.finditer(text):
        tokens.add(m.group())
    han = [c for c in chunks if "一" <= c <= "鿿"]
    for a, b in zip(han, han[1:]):
        tokens.add(a + b)
    for c in han:
        tokens.add(c)
    return {t for t in tokens if t not in STOPWORDS and len(t) > 0}


def content_tokens(content: VersionContent, synonyms: dict[str, str]
                   ) -> set[str]:
    parts = [
        content.title, content.equipment_model, content.process,
        " ".join(content.conditions), content.conclusion,
        " ".join(content.tags),
    ]
    normalized = normalize_text(" ".join(parts), synonyms)
    return _char_bigrams(normalized)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def score_pair(left: VersionContent, right: VersionContent,
               synonyms: dict[str, str]) -> float:
    # 型号完全不同则显著降权，但不直接归零（型号字段本身也可能写法不一）
    base = jaccard(content_tokens(left, synonyms),
                   content_tokens(right, synonyms))
    same_kind = left.kind == right.kind
    eq_norm_l = normalize_text(left.equipment_model, synonyms)
    eq_norm_r = normalize_text(right.equipment_model, synonyms)
    if eq_norm_l and eq_norm_r and eq_norm_l != eq_norm_r:
        base *= 0.5
    if not same_kind:
        base *= 0.8
    return round(base, 4)
