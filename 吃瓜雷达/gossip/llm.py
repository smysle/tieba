#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大模型适配层：OpenAI 兼容 / Anthropic / 本地 CLI。

三种都能跑，配置在网页「设置」里填，也可以走环境变量。
没有配置时 summarize 会自动退回本地规则引擎，不会把页面卡死。
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from . import util

PRESETS = {
    "openai": {"type": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "deepseek": {"type": "openai", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "moonshot": {"type": "openai", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-32k"},
    "qwen": {"type": "openai",
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "zhipu": {"type": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "siliconflow": {"type": "openai", "base_url": "https://api.siliconflow.cn/v1",
                    "model": "Qwen/Qwen2.5-72B-Instruct"},
    "anthropic": {"type": "anthropic", "base_url": "https://api.anthropic.com",
                  "model": "claude-3-5-sonnet-latest"},
    "cli": {"type": "cli", "base_url": "", "model": "claude-cli"},
}

ENV_KEYS = [
    ("OPENAI_API_KEY", "openai", "OPENAI_BASE_URL", "gpt-4o-mini"),
    ("DEEPSEEK_API_KEY", "deepseek", None, "deepseek-chat"),
    ("MOONSHOT_API_KEY", "moonshot", None, "moonshot-v1-32k"),
    ("DASHSCOPE_API_KEY", "qwen", None, "qwen-plus"),
    ("ZHIPUAI_API_KEY", "zhipu", None, "glm-4-flash"),
    ("ANTHROPIC_API_KEY", "anthropic", None, "claude-3-5-sonnet-latest"),
]


class LlmError(RuntimeError):
    pass


def from_env() -> Optional[dict]:
    for env_name, preset, base_env, model in ENV_KEYS:
        key = os.environ.get(env_name, "").strip()
        if not key:
            continue
        cfg = dict(PRESETS[preset])
        cfg["api_key"] = key
        if base_env and os.environ.get(base_env):
            cfg["base_url"] = os.environ[base_env].strip()
        cfg["model"] = os.environ.get("GUA_MODEL", "").strip() or model
        cfg["source"] = "env:" + env_name
        return cfg
    return None


def resolve(db) -> dict:
    """配置优先级：网页设置 > 环境变量。"""
    cfg = db.get_config("llm") if db else None
    if cfg and (cfg.get("api_key") or cfg.get("type") == "cli"):
        cfg = dict(cfg)
        cfg.setdefault("source", "设置页")
        if cfg.get("type") == "cli" and not cfg.get("command"):
            cfg["command"] = ["claude", "-p"]
        return cfg
    env_cfg = from_env()
    if env_cfg:
        return env_cfg
    if cfg and cfg.get("type") == "cli":
        return dict(cfg)
    return {"type": "none", "model": "", "source": "未配置"}


def public_status(cfg: dict, cli_available: bool = False) -> dict:
    kind = cfg.get("type") or "none"
    configured = kind == "cli" and cli_available or bool(kind != "none" and cfg.get("api_key"))
    return {
        "configured": configured,
        "type": kind,
        "model": cfg.get("model") or "",
        "base_url": cfg.get("base_url") or "",
        "key_hint": util.mask_secret(cfg.get("api_key") or ""),
        "source": cfg.get("source") or "",
        "presets": sorted(PRESETS.keys()),
    }


def cli_available(command) -> bool:
    binary = "claude"
    if isinstance(command, list) and command:
        binary = command[0]
    elif isinstance(command, str) and command.strip():
        binary = shlex.split(command)[0]
    path = os.environ.get("PATH", "")
    for folder in path.split(os.pathsep):
        candidate = os.path.join(folder, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return True
    return False


def _http_json(url: str, payload: dict, headers: Dict[str, str],
               timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:600]
        raise LlmError("模型接口 HTTP %d：%s" % (exc.code, body))
    except urllib.error.URLError as exc:
        raise LlmError("模型接口连不上：%s" % (exc.reason,))
    try:
        return json.loads(body)
    except ValueError:
        raise LlmError("模型接口返回非 JSON：%s" % body[:200])


def chat(cfg: dict, system: str, user: str, max_tokens: int = 4000,
         timeout: float = 240.0) -> Tuple[str, dict]:
    kind = (cfg.get("type") or "").lower()
    model = cfg.get("model") or ""
    started = util.now()

    if kind == "cli":
        command = cfg.get("command") or ["claude", "-p"]
        if isinstance(command, str):
            command = shlex.split(command)
        prompt = "%s\n\n%s" % (system, user)
        if any("{{prompt}}" in part for part in command):
            command = [part.replace("{{prompt}}", prompt) for part in command]
            stdin_text = ""
        else:
            stdin_text = prompt
        try:
            proc = subprocess.run(command, input=stdin_text, capture_output=True,
                                  text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LlmError("本地 CLI 调用失败：%s" % exc)
        text = (proc.stdout or "").strip()
        if proc.returncode != 0 and not text:
            raise LlmError("本地 CLI 退出码 %d：%s" % (proc.returncode,
                                                    (proc.stderr or "").strip()[:300]))
        if not text:
            raise LlmError("本地 CLI 没有返回内容")
        return text, {"provider": "cli", "model": model or "cli",
                      "seconds": round(util.now() - started, 1)}

    if kind == "anthropic":
        base = (cfg.get("base_url") or PRESETS["anthropic"]["base_url"]).rstrip("/")
        url = base + "/v1/messages"
        headers = {
            "content-type": "application/json",
            "x-api-key": cfg.get("api_key") or "",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": model or PRESETS["anthropic"]["model"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": float(cfg.get("temperature") or 0.8),
        }
        data = _http_json(url, payload, headers, timeout)
        parts = data.get("content") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if not text:
            raise LlmError("Anthropic 返回为空：%s" % json.dumps(data, ensure_ascii=False)[:200])
        return text, {"provider": "anthropic", "model": payload["model"],
                      "seconds": round(util.now() - started, 1),
                      "usage": data.get("usage") or {}}

    if kind == "openai":
        base = (cfg.get("base_url") or PRESETS["openai"]["base_url"]).rstrip("/")
        url = base + "/chat/completions"
        headers = {
            "content-type": "application/json",
            "authorization": "Bearer %s" % (cfg.get("api_key") or ""),
        }
        payload = {
            "model": model or PRESETS["openai"]["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": float(cfg.get("temperature") or 0.8),
            "max_tokens": max_tokens,
        }
        data = _http_json(url, payload, headers, timeout)
        try:
            text = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            raise LlmError("模型返回结构异常：%s" % json.dumps(data, ensure_ascii=False)[:300])
        return text, {"provider": "openai", "model": payload["model"],
                      "seconds": round(util.now() - started, 1),
                      "usage": data.get("usage") or {}}

    raise LlmError("没有可用的模型配置")


def test(db) -> dict:
    cfg = resolve(db)
    status = public_status(cfg, cli_available(cfg.get("command")))
    if not status["configured"]:
        return {"ok": False, "error": "还没配置模型：填 API Key，或把 type 设成 cli", "status": status}
    try:
        text, meta = chat(cfg, "你是连通性测试助手。", "只回复四个字：瓜田就绪", max_tokens=32,
                          timeout=90)
        return {"ok": True, "reply": text[:60], "meta": meta, "status": status}
    except LlmError as exc:
        return {"ok": False, "error": str(exc), "status": status}
