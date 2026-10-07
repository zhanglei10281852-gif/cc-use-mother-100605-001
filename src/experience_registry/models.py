"""领域模型：经验条目、不可变版本、内容校验与状态机。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import ValidationError
from .hashing import canonical_json, sha256_hex


def _canonical_text(text: str) -> str:
    """空白归一化：任意连续空白压成一个空格，并去掉中文/全角标点两侧空格。"""
    text = re.sub(r"\s+", " ", text).strip()
    cjk = r"一-鿿　-〿＀-￯"
    text = re.sub(rf"(?<=[{cjk}])\s|\s(?=[{cjk}])", "", text)
    return text

# ---- 版本状态（版本不可变，状态只能沿允许的方向迁移） ----
IN_REVIEW = "in_review"   # 待复核
ACTIVE = "active"         # 已生效，进入可检索目录
REJECTED = "rejected"     # 复核未通过
WITHDRAWN = "withdrawn"   # 提交人撤回
EXPIRED = "expired"       # 过期结论
SUPERSEDED = "superseded" # 被同条目的更新版本替代
MERGED = "merged"         # 作为重复经验并入合并条目

VERSION_STATUSES = {
    IN_REVIEW, ACTIVE, REJECTED, WITHDRAWN, EXPIRED, SUPERSEDED, MERGED,
}

# 可检索目录只包含生效版本
DIRECTORY_STATUSES = {ACTIVE}

# 终态：仍然保留内容与全部证据，可解释当时为何采用
TERMINAL_STATUSES = {REJECTED, WITHDRAWN, EXPIRED, SUPERSEDED, MERGED}

# 允许的状态迁移
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    IN_REVIEW: {ACTIVE, REJECTED, WITHDRAWN, MERGED},
    ACTIVE: {EXPIRED, SUPERSEDED, MERGED},
    REJECTED: set(),
    WITHDRAWN: set(),
    EXPIRED: set(),
    SUPERSEDED: set(),
    MERGED: set(),
}

KNOWLEDGE_KINDS = {"fault_judgement", "process_knowhow"}
EVIDENCE_KINDS = {"field_verification", "work_order", "manual", "other"}

ROLES = ("admin", "engineer", "reviewer")

# 内容字段（顺序即 canonical 哈希顺序，不可随意调整）
CONTENT_FIELDS = (
    "title",
    "kind",
    "equipment_model",
    "process",
    "conditions",
    "content",
    "conclusion",
    "evidence_sources",
    "tags",
)


def _clean_text(value: Any, field_name: str, *, required: bool = False,
                 max_len: int = 10000) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"字段 {field_name} 必须是字符串")
    text = value.strip()
    if required and not text:
        raise ValidationError(f"字段 {field_name} 不能为空")
    if len(text) > max_len:
        raise ValidationError(f"字段 {field_name} 长度超过 {max_len}")
    return text


def _clean_str_list(value: Any, field_name: str, *, required: bool = False
                     ) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValidationError(f"字段 {field_name} 必须是字符串列表")
    items = [v.strip() for v in value if v and v.strip()]
    # 去重但保序，避免同义重复表述制造噪音
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    if required and not deduped:
        raise ValidationError(f"字段 {field_name} 至少需要一项")
    return deduped


@dataclass(frozen=True)
class EvidenceSource:
    """证据来源：结论必须能追溯到具体的现场验证或单据。"""

    kind: str                       # field_verification / work_order / manual / other
    reference: str                  # 工单号、验证批次、手册章节等
    verified_at: str = ""           # YYYY-MM-DD，可空（如手册条目）
    note: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> "EvidenceSource":
        if not isinstance(raw, dict):
            raise ValidationError("证据来源必须是对象")
        kind = _clean_text(raw.get("kind"), "evidence.kind", required=True)
        if kind not in EVIDENCE_KINDS:
            raise ValidationError(
                f"证据类型 {kind} 不受支持，可选：{sorted(EVIDENCE_KINDS)}")
        reference = _clean_text(raw.get("reference"), "evidence.reference",
                                required=True, max_len=200)
        verified_at = _clean_text(raw.get("verified_at"), "evidence.verified_at",
                                  max_len=20)
        note = _clean_text(raw.get("note"), "evidence.note", max_len=2000)
        return cls(kind, reference, verified_at, note)

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "reference": self.reference,
            "verified_at": self.verified_at,
            "note": self.note,
        }


@dataclass(frozen=True)
class VersionContent:
    """一条经验版本的完整内容。对象不可变，哈希确定。"""

    title: str
    kind: str
    equipment_model: str
    process: str
    conditions: list[str]
    content: str
    conclusion: str
    evidence_sources: list[EvidenceSource] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Any) -> "VersionContent":
        if not isinstance(raw, dict):
            raise ValidationError("经验内容必须是对象")
        unknown = set(raw) - set(CONTENT_FIELDS)
        if unknown:
            raise ValidationError(f"存在未知字段：{sorted(unknown)}")
        kind = _clean_text(raw.get("kind"), "kind", required=True)
        if kind not in KNOWLEDGE_KINDS:
            raise ValidationError(
                f"经验类型 {kind} 不受支持，可选：{sorted(KNOWLEDGE_KINDS)}")
        evidence = [EvidenceSource.from_dict(v)
                    for v in (raw.get("evidence_sources") or [])]
        return cls(
            title=_clean_text(raw.get("title"), "title", required=True,
                              max_len=200),
            kind=kind,
            equipment_model=_clean_text(raw.get("equipment_model"),
                                        "equipment_model", required=True,
                                        max_len=100),
            process=_clean_text(raw.get("process"), "process", required=True,
                                max_len=100),
            conditions=_clean_str_list(raw.get("conditions"), "conditions",
                                       required=True),
            content=_clean_text(raw.get("content"), "content", required=True,
                                max_len=20000),
            conclusion=_clean_text(raw.get("conclusion"), "conclusion",
                                   required=True, max_len=5000),
            evidence_sources=evidence,
            tags=_clean_str_list(raw.get("tags"), "tags"),
        )

    def canonical(self) -> dict[str, Any]:
        """规范化表示：唯一决定 content_hash 的形式。"""
        return {
            "title": self.title,
            "kind": self.kind,
            "equipment_model": self.equipment_model.casefold(),
            "process": self.process.casefold(),
            "conditions": sorted(c.casefold() for c in self.conditions),
            "content": _canonical_text(self.content),
            "conclusion": _canonical_text(self.conclusion),
            "evidence_sources": sorted(
                (canonical_json(e.as_dict()) for e in self.evidence_sources)
            ),
            "tags": sorted(t.casefold() for t in self.tags),
        }

    @property
    def content_hash(self) -> str:
        return sha256_hex(canonical_json(self.canonical()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "kind": self.kind,
            "equipment_model": self.equipment_model,
            "process": self.process,
            "conditions": list(self.conditions),
            "content": self.content,
            "conclusion": self.conclusion,
            "evidence_sources": [e.as_dict() for e in self.evidence_sources],
            "tags": list(self.tags),
        }


def assert_transition(current: str, target: str) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValidationError(f"不允许从 {current} 转为 {target}")
