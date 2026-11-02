#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小工具：时间、文本、文件。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

TZ = 8 * 3600  # 展示统一按东八区


def now() -> float:
    return time.time()


def fmt_ts(ts, fmt: str = "%m-%d %H:%M") -> str:
    if not ts:
        return ""
    try:
        return time.strftime(fmt, time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def fmt_full(ts) -> str:
    return fmt_ts(ts, "%Y-%m-%d %H:%M:%S")


def fmt_date(ts) -> str:
    return fmt_ts(ts, "%Y-%m-%d")


def human_ago(ts) -> str:
    try:
        delta = max(0.0, time.time() - float(ts or 0))
    except (TypeError, ValueError):
        return ""
    if delta < 45:
        return "刚刚"
    if delta < 3600:
        return "%d 分钟前" % int(delta // 60)
    if delta < 86400:
        return "%d 小时前" % int(delta // 3600)
    return "%d 天前" % int(delta // 86400)


def human_span(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        return "%d 分钟" % (seconds // 60)
    if seconds < 86400:
        return "%d 小时 %d 分" % (seconds // 3600, (seconds % 3600) // 60)
    return "%d 天 %d 小时" % (seconds // 86400, (seconds % 86400) // 3600)


_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def normalize_space(text: str) -> str:
    text = _WS_RE.sub(" ", str(text or ""))
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def clip(text: str, limit: int = 280, ellipsis: str = "…") -> str:
    text = normalize_space(text)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - len(ellipsis))].rstrip() + ellipsis


def one_line(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return clip(text, limit)


def stable_hash(text: str, mod: int = 1000) -> int:
    """跨进程稳定的哈希（Python 内置 hash 带随机盐，不能用来做模板选择）。"""
    digest = hashlib.md5(str(text).encode("utf-8", "replace")).hexdigest()
    return int(digest[:12], 16) % max(1, mod)


def sha1(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()


def safe_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def atomic_write(path: str, data) -> None:
    """先写临时文件再 rename，避免半截文件。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: str, obj) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2))


def json_loads(text, default=None):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def mask_secret(secret: str) -> str:
    """只回显尾部 4 位，前端不需要知道完整 key。"""
    secret = (secret or "").strip()
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:4] + "…" + secret[-4:]


def parse_tid(raw: str):
    """从链接 / tid / 纯数字里抠出 tid。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    match = re.search(r"(?:/p/|tid=|kz=)(\d{5,})", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d{8,})\b", text)
    return int(match.group(1)) if match else None


def parse_interval(raw, default: int = 120) -> int:
    value = safe_int(raw, default)
    return max(15, min(3600, value))
