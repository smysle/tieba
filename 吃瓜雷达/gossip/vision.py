#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片理解：多模态 API 优先，macOS Vision 本地 OCR 兜底。

输出统一结构：
  {"engine","type","ocr_text","description","entities","amounts","timestamps","confidence"}
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Optional

from . import util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

PROMPT = """你是贴吧吃瓜助手，请阅读这张帖子图片，严格输出 JSON：
{
  "type": "chat|transaction|document|photo|meme|other",
  "ocr_text": "图中所有可读文字，保持原有换行和说话人顺序；看不清的用 [?] 标记",
  "description": "一句话描述画面内容",
  "entities": ["出现的昵称/公司/地点/物品"],
  "amounts": ["金额或关键数字"],
  "timestamps": ["图中出现的时间"],
  "confidence": 0.0
}
要求：
1. 只依据图片里可见的内容，禁止推测图中没有的信息；
2. 聊天截图请尽量保留「谁在说话 + 时间 + 原话」；
3. 表情包/无字图 ocr_text 留空，用 description 描述；
4. 只输出 JSON，不要解释。"""


class VisionError(RuntimeError):
    pass


def config_of(db) -> dict:
    cfg = db.get_config("vision", {}) or {}
    cfg.setdefault("type", "openai")
    cfg.setdefault("base_url", "https://api.openai.com/v1")
    cfg.setdefault("model", "gpt-4o-mini")
    if "enabled" not in cfg:
        cfg["enabled"] = True if cfg.get("type") == "cli" else bool(cfg.get("api_key"))
    return cfg


def configured(cfg: dict) -> bool:
    if not cfg or not cfg.get("enabled"):
        return False
    if (cfg.get("type") or "").lower() == "cli":
        return True
    return bool(cfg.get("api_key") and cfg.get("base_url"))


def _mime_of(path: str) -> str:
    try:
        from PIL import Image
        with Image.open(path) as img:
            fmt = (img.format or "JPEG").lower()
        return {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
                "gif": "image/gif"}.get(fmt, "image/jpeg")
    except Exception:
        return "image/jpeg"


def _extract_json(text: str) -> dict:
    """从模型输出里找最后一个合法的 JSON 对象（CLI 会混入流式日志）。"""
    text = (text or "").strip()
    candidates = []
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start:index + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            candidates.append(obj)
                    except ValueError:
                        pass
                    break
    for obj in reversed(candidates):
        if "ocr_text" in obj or "type" in obj or "description" in obj:
            return obj
    return {"type": "other", "ocr_text": text[:2000], "description": "",
            "entities": [], "amounts": [], "timestamps": [], "confidence": 0.3}


def _post_json(url: str, payload: dict, headers: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise VisionError("识图接口 HTTP %d：%s" % (exc.code, body))
    except (urllib.error.URLError, ValueError) as exc:
        raise VisionError("识图接口失败：%s" % exc)


def _call_openai(cfg: dict, image_b64: str, mime: str, context: str) -> dict:
    base = (cfg.get("base_url") or "").rstrip("/")
    url = base + "/chat/completions"
    prompt = PROMPT + ("\n\n帖子上下文（仅辅助理解，不要据此编造）：\n" + context[:800] if context else "")
    payload = {
        "model": cfg.get("model") or "gpt-4o-mini",
        "temperature": 0.1,
        "max_tokens": 1600,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:%s;base64,%s" % (mime, image_b64)}},
            ],
        }],
    }
    data = _post_json(url, payload, {
        "content-type": "application/json",
        "authorization": "Bearer %s" % cfg.get("api_key"),
    })
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise VisionError("识图返回结构异常：%s" % json.dumps(data, ensure_ascii=False)[:200])
    out = _extract_json(text)
    out["engine"] = "vlm:" + (cfg.get("model") or "openai")
    return out


def _call_cli(cfg: dict, path: str, context: str) -> dict:
    command = cfg.get("command") or ["claude", "-p"]
    if isinstance(command, str):
        command = command.split()
    prompt = (PROMPT + "\n图片绝对路径：%s\n请先用 Read 工具读取这张图片，再按要求输出 JSON。"
              % path)
    if context:
        prompt += "\n帖子上下文（仅辅助，不要编造）：" + context[:600]
    last = ""
    for attempt in (1, 2):
        try:
            proc = subprocess.run(command, input=prompt, capture_output=True,
                                  text=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            last = str(exc)
            continue
        text = (proc.stdout or "").strip()
        if not text:
            last = (proc.stderr or "CLI 没有输出")[:200]
            continue
        if "API Error" in text or "malformed" in text.lower():
            last = util.clip(text, 200)
            continue
        out = _extract_json(text)
        if not out.get("ocr_text") and not out.get("description") and out.get("type") == "other":
            last = "CLI 没有给出可用 JSON"
            continue
        out["engine"] = "cli:" + (cfg.get("model") or "claude")
        return out
    raise VisionError("识图 CLI 失败：%s" % last)


def _call_anthropic(cfg: dict, image_b64: str, mime: str, context: str) -> dict:
    base = (cfg.get("base_url") or "https://api.anthropic.com").rstrip("/")
    url = base + "/v1/messages"
    prompt = PROMPT + ("\n\n帖子上下文（仅辅助理解，不要据此编造）：\n" + context[:800] if context else "")
    payload = {
        "model": cfg.get("model") or "claude-3-5-sonnet-latest",
        "max_tokens": 1600,
        "temperature": 0.1,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": mime, "data": image_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    data = _post_json(url, payload, {
        "content-type": "application/json",
        "x-api-key": cfg.get("api_key") or "",
        "anthropic-version": "2023-06-01",
    })
    text = "".join(part.get("text", "") for part in data.get("content") or []
                   if isinstance(part, dict))
    out = _extract_json(text)
    out["engine"] = "vlm:" + payload["model"]
    return out


# ---------------------------------------------------------------- 本地 OCR
def _helper_binary() -> str:
    src = os.path.join(ROOT, "tools", "vision_ocr.swift")
    binary = os.path.join(DATA, "bin", "vision_ocr")
    if os.path.exists(binary) and os.path.getmtime(binary) > os.path.getmtime(src):
        return binary
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    if not os.path.exists(swiftc):
        raise VisionError("没有 swiftc，无法启用本地 Vision OCR")
    util.ensure_dir(os.path.dirname(binary))
    proc = subprocess.run([swiftc, "-O", src, "-o", binary],
                          capture_output=True, text=True, timeout=240)
    if proc.returncode != 0 or not os.path.exists(binary):
        raise VisionError("编译 Vision OCR 失败：%s" % (proc.stderr or "")[:300])
    return binary


def local_ocr(path: str) -> str:
    binary = _helper_binary()
    proc = subprocess.run([binary, path], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise VisionError("本地 OCR 失败：%s" % (proc.stderr or "")[:200])
    return proc.stdout.strip()


def _guess_type(text: str, width: int = 0, height: int = 0) -> str:
    if re.search(r"转账|订单|支付|收款|余额|¥|￥", text):
        return "transaction"
    if re.search(r"\d{1,2}[:：]\d{2}", text) or "按住说话" in text:
        return "chat"
    if width and height and height / max(1, width) > 2.2:
        return "document"
    return "other"


def analyze(path: str, context: str = "", cfg: Optional[dict] = None,
            force_local: bool = False) -> dict:
    if not os.path.isfile(path):
        raise VisionError("图片不存在：%s" % path)
    mime = _mime_of(path)
    if cfg and configured(cfg) and not force_local:
        try:
            if (cfg.get("type") or "openai").lower() == "cli":
                return _call_cli(cfg, path, context)
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            if (cfg.get("type") or "openai").lower() == "anthropic":
                return _call_anthropic(cfg, b64, mime, context)
            return _call_openai(cfg, b64, mime, context)
        except VisionError as exc:
            result = _local_result(path)
            result["engine"] = "macos-vision（模型失败兜底）"
            result["error"] = str(exc)[:200]
            return result

    return _local_result(path)


def _local_result(path: str) -> dict:
    text = local_ocr(path)
    width = height = 0
    try:
        from PIL import Image
        with Image.open(path) as img:
            width, height = img.size
    except Exception:
        pass
    return {
        "engine": "macos-vision",
        "type": _guess_type(text, width, height),
        "ocr_text": text,
        "description": "",
        "entities": [],
        "amounts": [],
        "timestamps": re.findall(r"\d{1,2}[:：]\d{2}", text)[:8],
        "confidence": 0.55 if text else 0.2,
    }


def analyze_media(store, row: dict, cfg: dict, context: str = "") -> dict:
    """下载（如需）并识别一行 media，写回数据库。"""
    if row.get("status") != "done" and not row.get("path"):
        row = store.download(row)
    path = row.get("path") or ""
    if not path:
        raise VisionError("图片还没下载")
    result = analyze(path, context=context, cfg=cfg)
    store.db.update_media(int(row["id"]), analysis=result,
                          status="done", analyzed_at=util.now(), error="")
    row = dict(row)
    row["analysis"] = result
    return row
