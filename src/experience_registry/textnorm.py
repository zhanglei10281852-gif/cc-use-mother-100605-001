"""术语归一化与条目相似度。

不同班组对同一现象使用不同术语（如"主轴抱死"与"主轴卡死"），
先按别名表归一化，再对中文字符做二元组（bigram）相似度，
保证同一经验的不同叫法能被稳定地识别为相似条目。
相似度是纯函数：相同输入永远得到相同分数。
"""

import re

MODEL_WEIGHT = 0.30
PROCESS_WEIGHT = 0.20
CONTENT_WEIGHT = 0.35
CONDITIONS_WEIGHT = 0.15

SIMILARITY_THRESHOLD = 0.55

_PUNCT = re.compile(r"[\s，。、；：,.;:()（）【】\[\]{}<>《》/\\\-_—!！?？\"'“”‘’]+")


def normalize_text(text, alias_map=None):
    """归一化：小写、别名替换（长词优先）、标点折叠为单空格。"""
    t = (text or "").strip().lower()
    if alias_map:
        for alias in sorted(alias_map, key=len, reverse=True):
            t = t.replace(alias.lower(), str(alias_map[alias]).lower())
    t = _PUNCT.sub(" ", t)
    return t.strip()


def normalize_model(model):
    """设备型号归一化：忽略大小写与空白分隔。"""
    return re.sub(r"\s+", "", (model or "").strip().lower())


def _bigrams(text):
    chars = [c for c in text if not c.isspace()]
    if len(chars) == 1:
        return {chars[0]}
    return {"".join(chars[i : i + 2]) for i in range(len(chars) - 1)}


def _jaccard(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def text_similarity(a, b, alias_map=None):
    return _jaccard(_bigrams(normalize_text(a, alias_map)), _bigrams(normalize_text(b, alias_map)))


def model_similarity(a, b):
    ma, mb = normalize_model(a), normalize_model(b)
    if not ma or not mb:
        return 0.0
    if ma == mb:
        return 1.0
    return _jaccard(_bigrams(ma), _bigrams(mb))


def _version_text(version):
    terms = version.get("terms") or []
    return " ".join([version.get("content") or ""] + [str(t) for t in terms])


def entry_similarity(va, vb, alias_map=None):
    """对两个版本字段做加权相似度，返回 (总分, 各维度得分)。"""
    parts = {
        "equipment_model": model_similarity(va.get("equipment_model"), vb.get("equipment_model")),
        "process_step": text_similarity(va.get("process_step"), vb.get("process_step"), alias_map),
        "content": text_similarity(_version_text(va), _version_text(vb), alias_map),
        "applicable_conditions": text_similarity(
            va.get("applicable_conditions"), vb.get("applicable_conditions"), alias_map
        ),
    }
    score = (
        MODEL_WEIGHT * parts["equipment_model"]
        + PROCESS_WEIGHT * parts["process_step"]
        + CONTENT_WEIGHT * parts["content"]
        + CONDITIONS_WEIGHT * parts["applicable_conditions"]
    )
    reasons = {k: round(v, 4) for k, v in parts.items()}
    return round(score, 4), reasons
