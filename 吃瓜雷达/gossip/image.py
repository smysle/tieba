#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封面生图：接 OpenAI 兼容的 /images/generations。

不配置就退回本地 SVG 封面，报告页永远有图，不会白屏。
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

from . import util


class ImageError(RuntimeError):
    pass


def config_of(db) -> dict:
    cfg = db.get_config("image", {}) or {}
    cfg.setdefault("enabled", bool(cfg.get("api_key")))
    cfg.setdefault("size", "1024x1024")
    cfg.setdefault("model", "gpt-image-1")
    return cfg


def configured(cfg: dict) -> bool:
    return bool(cfg.get("enabled") and cfg.get("api_key") and cfg.get("base_url"))


def generate_cover(cfg: dict, prompt: str, out_path: str) -> dict:
    if not configured(cfg):
        return {"ok": False, "error": "未配置生图 API"}
    base = (cfg.get("base_url") or "").rstrip("/")
    url = base + "/images/generations"
    payload = {
        "model": cfg.get("model") or "gpt-image-1",
        "prompt": prompt,
        "n": 1,
        "size": cfg.get("size") or "1024x1024",
    }
    if cfg.get("response_format", "b64_json") == "b64_json":
        payload["response_format"] = "b64_json"
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer %s" % cfg.get("api_key"),
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise ImageError("生图接口 HTTP %d：%s" % (exc.code, body))
    except (urllib.error.URLError, ValueError) as exc:
        raise ImageError("生图接口失败：%s" % exc)

    item = (data.get("data") or [{}])[0]
    blob = None
    if item.get("b64_json"):
        blob = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        try:
            with urllib.request.urlopen(item["url"], timeout=90) as resp:
                blob = resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ImageError("封面下载失败：%s" % exc)
    if not blob:
        raise ImageError("生图接口没返回图片：%s" % json.dumps(data, ensure_ascii=False)[:200])

    util.ensure_dir(os.path.dirname(os.path.abspath(out_path)))
    with open(out_path, "wb") as fh:
        fh.write(blob)
    return {"ok": True, "path": out_path, "bytes": len(blob),
            "revised_prompt": item.get("revised_prompt", "")}
