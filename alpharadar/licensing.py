"""许可过滤：决定哪些策略的结论可以进对外（付费产品）导出。

判定来源（取最严）：
  1. 策略 @register 的 license 字段；
  2. 语料库里原 Pine 源码头部的许可声明（按 source_sid 回查，最可靠）；
  3. config/licenses.json 的作者兜底名单（前两者都判不了时才用）。

任一来源判为「非商用（CC BY-NC*）」或「禁止再分发」就排除；
都判不了时按 unknown_policy 处理（默认 allow，但标 license_status=unknown）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import CORPUS_DIR, ROOT

LICENSES_PATH = ROOT / "config" / "licenses.json"

_NC = re.compile(r"by[- ]?nc|non[- ]?commercial|\bNC\b", re.I)
_RESTRICTED = re.compile(r"non[- ]?redistributable|all rights reserved|proprietary|"
                         r"not\s+(?:be\s+)?(?:re)?distribut|禁止(?:商用|再分发|转载)", re.I)
_OPEN = re.compile(r"\bMIT\b|\bMPL\b|Mozilla Public License|Apache|\bBSD\b|\bGPL|"
                   r"\bCC0\b|public domain|CC[- ]BY(?:[- ]SA)?\b(?![- ]?NC)|原创", re.I)


def classify(text: str | None) -> str:
    """许可文本 → nc / restricted / open / unknown。"""
    t = (text or "").strip()
    if not t or t in {"—", "-", "?", "未知", "<按原脚本填写>"}:
        return "unknown"
    if _NC.search(t):
        return "nc"
    if _RESTRICTED.search(t):
        return "restricted"
    if _OPEN.search(t):
        return "open"
    return "unknown"


def load_policy(path: Path | None = None) -> dict:
    p = Path(path or LICENSES_PATH)
    if not p.exists():
        return {"unknown_policy": "allow", "nc_authors": []}
    return json.loads(p.read_text(encoding="utf-8"))


def corpus_header(sid: str, max_lines: int = 40) -> str:
    """按语料库 sid 读原 Pine 源码头部的许可/版权行；拿不到返回空串。"""
    if not sid:
        return ""
    try:
        from . import store
        rec = store.get_script(sid)
    except Exception:
        rec = None
    if not rec or not rec.get("file"):
        return ""
    fname = os.path.basename(str(rec["file"]))
    f = CORPUS_DIR / "sources" / fname
    if not f.is_file():
        return ""
    lines = []
    with f.open(encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= max_lines:
                break
            if re.search(r"licen|copyright|©|creativecommons|mozilla", line, re.I):
                lines.append(line.strip())
    return "\n".join(lines)


@dataclass
class LicenseInfo:
    status: str            # open / nc / restricted / unknown
    commercial_ok: bool
    label: str             # 对外展示的许可文字
    basis: str             # 判定依据：declared / corpus / author / none


def resolve(strategy, policy: dict | None = None,
            header_lookup: Callable[[str], str] | None = None) -> LicenseInfo:
    """判定一个已注册策略的许可。strategy 需有 license / source / source_sid 属性。"""
    policy = policy if policy is not None else load_policy()
    lookup = header_lookup or corpus_header
    declared = str(getattr(strategy, "license", "") or "")
    source = str(getattr(strategy, "source", "") or "")
    sid = str(getattr(strategy, "source_sid", "") or "")

    found: list[tuple[str, str, str]] = []           # (status, basis, label)
    d = classify(declared)
    if d != "unknown":
        found.append((d, "declared", declared))
    if sid:
        h = lookup(sid)
        hc = classify(h)
        if hc != "unknown":
            found.append((hc, "corpus", h.splitlines()[0][:120] if h else ""))
    for st in ("nc", "restricted"):
        for status, basis, label in found:
            if status == st:
                return LicenseInfo(st, False, label or declared, basis)
    if found:
        status, basis, label = found[0]
        return LicenseInfo("open", True, declared if basis == "declared" else label, basis)
    authors = [a.lower() for a in policy.get("nc_authors", [])]
    if any(a and a in source.lower() for a in authors):
        return LicenseInfo("nc", False, "疑似 CC BY-NC-SA（作者兜底名单）", "author")
    allow = str(policy.get("unknown_policy", "allow")).lower() == "allow"
    return LicenseInfo("unknown", allow, declared or "未声明", "none")
