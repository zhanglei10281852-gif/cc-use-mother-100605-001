"""确定性哈希工具：相同输入在任何进程、任何时间都得到相同结果。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def digest_parts(parts: list[Any]) -> str:
    """对按固定顺序排列的若干字段做稳定哈希，字段顺序本身参与计算。"""
    h = hashlib.sha256()
    for part in parts:
        h.update(canonical_json(part).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def short_id(prefix: str, parts: list[Any], length: int = 16) -> str:
    return f"{prefix}_{digest_parts(parts)[:length]}"
