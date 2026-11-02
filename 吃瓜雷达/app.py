#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""瓜田雷达 · 本地服务。

零依赖（Python 3.8+ 标准库），`python3 app.py` 就能跑：
  - 监控贴吧帖子 / 贴吧吧 / 贴吧热榜
  - 抓楼层、楼中楼、事件流、热度曲线
  - 四种切瓜报告（本地规则 / 大模型）
  - 报告页可截图、可导出长图、可复制 Markdown
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

from gossip import db, image, linker, llm, media, monitor, prompts, render, summarize, tieba, util, vision

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(ROOT, "static")
DATA_DIR = os.path.join(ROOT, "data")
VERSION = "1.0.0"


# --------------------------------------------------------------------- 任务
class Job:
    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.status = "running"
        self.logs = []
        self.result = None
        self.error = ""
        self.created_at = time.time()
        self.finished_at = 0.0

    def log(self, message: str) -> None:
        self.logs.append({"ts": time.time(), "text": message})
        self.logs = self.logs[-60:]

    def done(self, result) -> None:
        self.status = "done"
        self.result = result
        self.finished_at = time.time()

    def fail(self, error: str) -> None:
        self.status = "error"
        self.error = error
        self.finished_at = time.time()

    def out(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "logs": self.logs, "result": self.result, "error": self.error,
            "created_at": self.created_at, "finished_at": self.finished_at,
        }


class Jobs:
    def __init__(self, app) -> None:
        self.app = app
        self._lock = threading.Lock()
        self._items: Dict[str, Job] = {}

    def submit(self, kind: str, fn) -> Job:
        job = Job(kind)
        with self._lock:
            self._items[job.id] = job
            # 只保留最近 40 个
            if len(self._items) > 40:
                for key in sorted(self._items, key=lambda k: self._items[k].created_at)[:10]:
                    self._items.pop(key, None)

        def runner():
            try:
                job.done(fn(job))
            except Exception as exc:
                self.app.log.exception("任务失败")
                job.fail("%s: %s" % (type(exc).__name__, exc))

        threading.Thread(target=runner, daemon=True, name="job-" + kind).start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._items.get(job_id)


# --------------------------------------------------------------------- 应用
class App:
    def __init__(self, db_path: str, enable_monitor: bool = True) -> None:
        self.log = logging.getLogger("guatan")
        self.db = db.Db(db_path)
        self.monitor = monitor.Monitor(self.db, logger=self.log)
        self.jobs = Jobs(self)
        self.enable_monitor = enable_monitor
        self.media = media.MediaStore(self.db, os.path.join(DATA_DIR, "media"))
        util.ensure_dir(os.path.join(DATA_DIR, "covers"))
        util.ensure_dir(os.path.join(DATA_DIR, "media"))
        if enable_monitor:
            self.monitor.start()
        self.log.info("数据文件：%s", os.path.abspath(db_path))

    def shutdown(self) -> None:
        try:
            self.monitor.stop()
        finally:
            self.db.close()


# -------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore
    server_version = "Guatan/" + VERSION

    # ------------------------------------------------------------- 基础输出
    def log_message(self, fmt, *args):  # noqa: A003
        logging.getLogger("guatan.http").debug(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str,
              extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json({"ok": False, "error": message}, code)

    def _text(self, text: str, content_type: str = "text/plain; charset=utf-8",
              filename: str = "") -> None:
        headers = {"Content-Disposition": 'inline; filename="%s"' % filename} if filename else {}
        self._send(200, text.encode("utf-8"), content_type, headers)

    def _body(self) -> dict:
        length = util.safe_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return {}
        if length > 4 * 1024 * 1024:
            raise ValueError("请求体太大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求体不是合法 JSON")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------- 路由入口
    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")

    def do_HEAD(self):  # noqa: N802
        self._dispatch("GET")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._api(method, path, query)
            elif path == "/" or path == "/index.html":
                self._static("index.html")
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path.startswith("/covers/"):
                self._file(os.path.join(DATA_DIR, "covers"), path[len("/covers/"):])
            elif path.startswith("/media/"):
                self._file(os.path.join(DATA_DIR, "media"), path[len("/media/"):])
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._error(404, "没有这个页面")
        except ValueError as exc:
            self._error(400, str(exc))
        except BrokenPipeError:
            pass
        except Exception as exc:  # 兜底，别让服务挂掉
            self.app.log.exception("请求处理失败 %s %s", method, path)
            self._error(500, "%s: %s" % (type(exc).__name__, exc))

    def _static(self, name: str) -> None:
        self._file(STATIC_DIR, name)

    def _file(self, base: str, name: str) -> None:
        name = urllib.parse.unquote(name)
        target = os.path.abspath(os.path.join(base, name))
        base_abs = os.path.abspath(base)
        if not target.startswith(base_abs + os.sep) or not os.path.isfile(target):
            self._error(404, "文件不存在")
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype)

    # ---------------------------------------------------------------- API
    def _api(self, method: str, path: str, query: dict) -> None:
        parts = [p for p in path.strip("/").split("/") if p]
        # /api/...
        if parts[:1] != ["api"]:
            self._error(404, "未知接口")
            return
        parts = parts[1:]
        route = "/".join(parts)

        if method == "GET":
            if route == "state":
                return self._json(self._state())
            if route == "health":
                return self._json({"ok": True, "version": VERSION, "ts": time.time()})
            if route == "modes":
                return self._json({"ok": True, "modes": prompts.mode_meta()})
            if route == "hot":
                hot_src = next((s for s in self.app.db.list_sources()
                                if s["kind"] == "hot"), None)
                topics = (hot_src or {}).get("meta", {}).get("hot") or []
                return self._json({"ok": True, "updated": (hot_src or {}).get("last_ok", 0),
                                   "topics": topics})
            if route == "stories":
                return self._json({"ok": True, "stories": self.app.db.list_stories()})
            if len(parts) >= 2 and parts[0] == "stories":
                return self._story_detail(parts[1])
            if len(parts) >= 3 and parts[0] == "threads" and parts[2] == "media":
                return self._thread_media(parts[1])
            if route == "events":
                limit = util.safe_int((query.get("limit") or ["60"])[0], 60)
                return self._json({"ok": True,
                                   "events": self.app.db.list_events(min(limit, 300))})
            if route == "reports":
                tid = util.safe_int((query.get("tid") or ["0"])[0], 0) or None
                return self._json({"ok": True, "reports": self.app.db.list_reports(tid)})
            if len(parts) >= 2 and parts[0] == "threads":
                return self._thread_detail(parts[1], query)
            if len(parts) >= 2 and parts[0] == "reports":
                return self._report(parts[1], parts[2] if len(parts) > 2 else "")
            if len(parts) >= 2 and parts[0] == "jobs":
                job = self.app.jobs.get(parts[1])
                if not job:
                    return self._error(404, "任务不存在或已过期")
                return self._json({"ok": True, "job": job.out()})
            if route == "config":
                return self._json({"ok": True, "config": self._config_view(secret=True)})
            self._error(404, "未知接口 GET /api/%s" % route)
            return

        if method == "POST":
            body = self._body()
            if route == "sources":
                return self._add_source(body)
            if len(parts) == 3 and parts[0] == "sources" and parts[2] in ("toggle", "poll"):
                return self._source_action(parts[1], parts[2])
            if len(parts) == 3 and parts[0] == "threads" and parts[2] == "vision":
                return self._vision(parts[1], body)
            if len(parts) == 3 and parts[0] == "stories" and parts[2] == "summarize":
                return self._story_summarize(parts[1], body)
            if route == "stories/build":
                return self._stories_build(body)
            if route == "forum/crawl":
                return self._forum_crawl(body)
            if len(parts) >= 3 and parts[0] == "floors" and parts[-1] == "star":
                return self._star(parts, body)
            if route == "summarize":
                return self._summarize(body)
            if route == "config":
                return self._save_config(body)
            if route == "config/test":
                return self._json({"ok": True, "result": llm.test(self.app.db)})
            if route == "import":
                return self._import(body)
            self._error(404, "未知接口 POST /api/%s" % route)
            return

        if method == "DELETE":
            if len(parts) == 2 and parts[0] == "threads":
                tid = util.safe_int(parts[1])
                if not self.app.db.get_thread(tid):
                    return self._error(404, "这个帖不在库里")
                self.app.db.delete_thread(tid)
                return self._json({"ok": True})
            if len(parts) == 2 and parts[0] == "sources":
                sid = util.safe_int(parts[1])
                if not self.app.db.get_source(sid):
                    return self._error(404, "监控源不存在")
                self.app.db.delete_source(sid)
                return self._json({"ok": True})
            self._error(404, "未知接口 DELETE")
            return

        self._error(405, "方法不支持")

    # ------------------------------------------------------------ 状态接口
    def _state(self) -> dict:
        database = self.app.db
        threads = database.list_threads()
        stories = database.list_stories()
        thread_map = {int(t["tid"]): t for t in threads}
        sources = database.list_sources()
        for src in sources:
            series = database.poll_series(int(src["id"]), 30)
            src["spark"] = [int(p.get("new_floors") or 0) for p in series][-24:]
            src["latest"] = database.latest_poll(int(src["id"]))
            src["new_recent"] = sum(src["spark"][-12:])
            if src["kind"] == "thread":
                src["thread"] = (thread_map.get(util.safe_int(src["key"]))
                                 or database.get_thread(util.safe_int(src["key"])))
                if src["thread"]:
                    src["title"] = src["thread"].get("title") or src["title"]
        events = database.list_events(60)
        floor_total = sum(int(t.get("floors_now") or 0) for t in threads)
        cfg = llm.resolve(database)
        return {
            "ok": True,
            "monitor": self.app.monitor.status(),
            "sources": sources,
            "threads": threads,
            "stories": stories,
            "events": events,
            "modes": prompts.mode_meta(),
            "config": self._config_view(),
            "llm": llm.public_status(cfg, llm.cli_available(cfg.get("command"))),
            "stats": {
                "sources": len(sources),
                "threads": len(threads),
                "floors": floor_total,
                "reports": len(database.list_reports(limit=100)),
                "stories": len(stories),
                "events": len(events),
            },
            "version": VERSION,
        }

    def _config_view(self, secret: bool = False) -> dict:
        database = self.app.db
        tieba_cfg = database.get_config("tieba", {}) or {}
        llm_cfg = database.get_config("llm", {}) or {}
        image_cfg = image.config_of(database)
        vision_cfg = vision.config_of(database)
        mon_cfg = database.get_config("monitor", {}) or {}
        return {
            "tieba": {
                "has_cookie": bool(tieba_cfg.get("cookie")),
                "cookie_hint": util.mask_secret(tieba_cfg.get("cookie", "")),
                "delay": tieba_cfg.get("delay", 0.35),
            },
            "llm": {
                "type": llm_cfg.get("type", ""),
                "base_url": llm_cfg.get("base_url", ""),
                "model": llm_cfg.get("model", ""),
                "key_hint": util.mask_secret(llm_cfg.get("api_key", "")),
                "command": llm_cfg.get("command") or [],
                "temperature": llm_cfg.get("temperature", 0.8),
                "presets": {k: {"base_url": v["base_url"], "model": v["model"], "type": v["type"]}
                            for k, v in llm.PRESETS.items()},
            },
            "image": {
                "enabled": bool(image_cfg.get("enabled")),
                "base_url": image_cfg.get("base_url", ""),
                "model": image_cfg.get("model", ""),
                "size": image_cfg.get("size", "1024x1024"),
                "key_hint": util.mask_secret(image_cfg.get("api_key", "")),
                "configured": image.configured(image_cfg),
            },
            "monitor": {
                "max_pages": mon_cfg.get("max_pages", 40),
                "keywords": mon_cfg.get("keywords") or monitor.DEFAULT_KEYWORDS,
            },
            "vision": {
                "type": vision_cfg.get("type", "openai"),
                "base_url": vision_cfg.get("base_url", ""),
                "model": vision_cfg.get("model", ""),
                "command": vision_cfg.get("command") or [],
                "enabled": bool(vision_cfg.get("enabled")),
                "configured": vision.configured(vision_cfg),
                "key_hint": util.mask_secret(vision_cfg.get("api_key", "")),
            },
        }

    # ------------------------------------------------------------ 监控源
    def _add_source(self, body: dict) -> None:
        kind = (body.get("kind") or "thread").strip()
        raw_key = str(body.get("key") or "").strip()
        if kind not in ("thread", "forum", "hot"):
            raise ValueError("kind 只能是 thread / forum / hot")
        if kind == "thread":
            tid = util.parse_tid(raw_key)
            if not tid:
                raise ValueError("给个贴吧链接或 tid，比如 https://tieba.baidu.com/p/11154226751")
            key = str(tid)
        elif kind == "forum":
            key = raw_key.lstrip("贴吧").strip()
            if not key:
                raise ValueError("吧名不能为空")
        else:
            key = "hot"

        meta = {}
        for field in ("keyword", "min_replies", "keywords"):
            if body.get(field) not in (None, "", []):
                meta[field] = body[field]
        interval = util.parse_interval(body.get("interval"), 120 if kind == "thread" else 300)
        title = body.get("title") or ("贴吧热榜" if kind == "hot" else
                                      ("吧：%s" % key if kind == "forum" else "帖 %s" % key))
        src = self.app.db.add_source(kind, key, title=title, note=body.get("note") or "",
                                     interval=interval, meta=meta)
        self.app.monitor.kick(int(src["id"]))
        self.app.db.add_event("watch", "开始盯：%s" % title, source_id=src["id"],
                              tid=util.safe_int(key) if kind == "thread" else None)
        self._json({"ok": True, "source": src})

    def _source_action(self, sid: str, action: str) -> None:
        source_id = util.safe_int(sid)
        src = self.app.db.get_source(source_id)
        if not src:
            raise ValueError("监控源不存在")
        if action == "toggle":
            enabled = 0 if src.get("enabled") else 1
            self.app.db.update_source(source_id, enabled=enabled,
                                      status="idle" if enabled else "paused")
            if enabled:
                self.app.monitor.kick(source_id)
            self._json({"ok": True, "enabled": bool(enabled)})
            return
        self.app.monitor.kick(source_id)
        self._json({"ok": True, "message": "已排队，马上抓"})

    # ------------------------------------------------------------- 楼层
    def _thread_detail(self, tid_raw: str, query: dict) -> None:
        tid = util.safe_int(tid_raw)
        thread = self.app.db.get_thread(tid)
        if not thread:
            return self._error(404, "这个帖还没入库")
        mode = (query.get("mode") or ["all"])[0]
        text = (query.get("q") or [""])[0]
        limit = min(util.safe_int((query.get("limit") or ["80"])[0], 80), 400)
        offset = max(util.safe_int((query.get("offset") or ["0"])[0], 0), 0)
        floors = self.app.db.grouped_floors(tid, limit=limit + 1, offset=offset,
                                            mode=mode, query=text)
        has_more = len(floors) > limit
        floors = floors[:limit]
        self._json({
            "ok": True,
            "thread": thread,
            "stats": self.app.db.thread_stats(tid),
            "floors": floors,
            "has_more": has_more,
            "reports": self.app.db.list_reports(tid, limit=10),
        })

    def _star(self, parts, body: dict) -> None:
        # /api/floors/<tid>/<floor>/star
        tid = util.safe_int(parts[1])
        floor = util.safe_int(parts[2])
        self.app.db.set_star(tid, floor, util.safe_int(body.get("sub_id")), bool(body.get("starred")))
        self._json({"ok": True, "tid": tid, "floor": floor,
                    "starred": bool(body.get("starred"))})

    # ------------------------------------------------------------- 切瓜
    def _summarize(self, body: dict) -> None:
        tid = util.safe_int(body.get("tid"))
        if not tid:
            raise ValueError("缺少 tid")
        mode = body.get("mode") or "daily"
        if mode not in prompts.MODES:
            raise ValueError("未知模式：%s" % mode)
        provider = body.get("provider") or "auto"
        neutral = bool(body.get("neutral"))
        extra = str(body.get("extra") or "")[:500]
        app = self.app

        def work(job: Job):
            job.log("读取楼层…")
            report = summarize.generate(app.db, tid, mode, provider, neutral, extra,
                                        progress=job.log)
            job.log("报告生成完成：%s" % report["title"])
            thread = app.db.get_thread(tid) or {}
            stats = summarize.compute_stats(app.db.grouped_floors(tid))
            cover_path = ""
            img_cfg = image.config_of(app.db)
            if image.configured(img_cfg):
                job.log("调用生图 API 做封面…")
                try:
                    out = os.path.join(DATA_DIR, "covers", report["id"] + ".png")
                    res = image.generate_cover(img_cfg,
                                               prompts.build_cover_prompt(thread, stats), out)
                    if res.get("ok"):
                        cover_path = "/covers/%s.png" % report["id"]
                        app.db._exec("UPDATE reports SET cover=? WHERE id=?",
                                     (cover_path, report["id"]))
                        job.log("封面已生成")
                except Exception as exc:
                    job.log("封面失败（用本地封面兜底）：%s" % exc)
            return {
                "report_id": report["id"],
                "title": report["title"],
                "cover": cover_path,
                "meta": report["meta"],
                "preview": report["markdown"][:1200],
            }

        job = self.app.jobs.submit("summarize", work)
        self._json({"ok": True, "job_id": job.id})

    def _report(self, report_id: str, fmt: str) -> None:
        report = self.app.db.get_report(report_id)
        if not report:
            return self._error(404, "报告不存在")
        thread = self.app.db.get_thread(util.safe_int(report.get("tid")))
        if fmt == "html":
            cover = report.get("cover") or ""
            page = render.report_page(report, thread, cover)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if fmt in ("md", "markdown"):
            return self._text(report.get("markdown") or "",
                              "text/markdown; charset=utf-8",
                              filename="guatan-%s.md" % report_id)
        self._json({"ok": True, "report": {k: v for k, v in report.items() if k != "markdown"},
                    "markdown": report.get("markdown")})

    # ------------------------------------------------------------- 配置
    def _save_config(self, body: dict) -> None:
        database = self.app.db
        if isinstance(body.get("llm"), dict):
            incoming = dict(body["llm"])
            current = database.get_config("llm", {}) or {}
            preset = incoming.pop("preset", "")
            if preset and preset in llm.PRESETS:
                for key, value in llm.PRESETS[preset].items():
                    incoming.setdefault(key, value)
            key = incoming.get("api_key")
            if key == "__clear__":
                incoming.pop("api_key", None)
            elif not key:
                if current.get("api_key"):
                    incoming["api_key"] = current["api_key"]
                else:
                    incoming.pop("api_key", None)
            if incoming.get("command") and isinstance(incoming["command"], str):
                incoming["command"] = incoming["command"].split()
            merged = dict(current)
            merged.update({k: v for k, v in incoming.items() if v is not None})
            database.set_config("llm", merged)
        if isinstance(body.get("tieba"), dict):
            incoming = dict(body["tieba"])
            current = database.get_config("tieba", {}) or {}
            if incoming.get("cookie") == "__clear__":
                current.pop("cookie", None)
            elif incoming.get("cookie"):
                cookie = incoming["cookie"].strip()
                if not cookie.upper().startswith("BDUSS="):
                    cookie = "BDUSS=" + cookie
                current["cookie"] = cookie
            if incoming.get("delay") is not None:
                current["delay"] = max(0.0, min(3.0, util.safe_float(incoming["delay"], 0.35)))
            database.set_config("tieba", current)
        if isinstance(body.get("monitor"), dict):
            incoming = dict(body["monitor"])
            current = database.get_config("monitor", {}) or {}
            if incoming.get("max_pages") is not None:
                current["max_pages"] = max(5, min(200, util.safe_int(incoming["max_pages"], 40)))
            if isinstance(incoming.get("keywords"), list):
                current["keywords"] = [str(k).strip() for k in incoming["keywords"] if str(k).strip()]
            database.set_config("monitor", current)
        if isinstance(body.get("vision"), dict):
            incoming = dict(body["vision"])
            current = database.get_config("vision", {}) or {}
            if incoming.get("api_key") == "__clear__":
                current.pop("api_key", None)
            elif incoming.get("api_key"):
                current["api_key"] = incoming["api_key"].strip()
            for field in ("type", "base_url", "model", "command", "enabled"):
                if field in incoming:
                    current[field] = incoming[field]
            if isinstance(current.get("command"), str):
                current["command"] = current["command"].split()
            if "enabled" in incoming:
                current["enabled"] = bool(incoming["enabled"])
            else:
                current["enabled"] = (current.get("type") == "cli"
                                      or bool(current.get("api_key")))
            database.set_config("vision", current)
        if isinstance(body.get("image"), dict):
            incoming = dict(body["image"])
            current = database.get_config("image", {}) or {}
            if incoming.get("api_key") == "__clear__":
                current.pop("api_key", None)
            elif incoming.get("api_key"):
                current["api_key"] = incoming["api_key"].strip()
            for field in ("base_url", "model", "size", "enabled"):
                if field in incoming:
                    current[field] = incoming[field]
            if "enabled" in incoming:
                current["enabled"] = bool(incoming["enabled"])
            else:
                current["enabled"] = bool(current.get("api_key"))
            database.set_config("image", current)
        self._json({"ok": True, "config": self._config_view()})

    # ------------------------------------------------------------- 媒体
    def _media_url(self, path: str) -> str:
        if not path:
            return ""
        root = os.path.join(DATA_DIR, "media")
        try:
            rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
        except ValueError:
            return ""
        if rel.startswith(".."):
            return ""
        return "/media/" + rel.replace(os.sep, "/")

    def _media_out(self, row: dict) -> dict:
        out = dict(row)
        out["url"] = self._media_url(row.get("path"))
        out["thumb_url"] = self._media_url(row.get("thumb")) or out["url"]
        out["analysis"] = row.get("analysis") or {}
        return out

    def _thread_media(self, tid_raw: str) -> None:
        tid = util.safe_int(tid_raw)
        rows = self.app.db.media_rows(tid, limit=600)
        self._json({"ok": True, "media": [self._media_out(r) for r in rows],
                    "counts": self.app.db.media_count(tid)})

    def _vision(self, tid_raw: str, body: dict) -> None:
        tid = util.safe_int(tid_raw)
        limit = min(60, max(1, util.safe_int(body.get("limit"), 6)))
        force = bool(body.get("force"))
        engine = (body.get("engine") or "auto").lower()
        app = self.app
        thread = app.db.get_thread(tid)
        if not thread:
            raise ValueError("帖子不存在")
        cfg = vision.config_of(app.db)
        if engine == "local":
            cfg = dict(cfg)
            cfg["enabled"] = False

        def work(job: Job):
            app.media.register_thread(tid)
            rows = [r for r in app.db.media_rows(tid, limit=600)
                    if force or not r.get("analysis")]
            rows = rows[:limit]
            job.log("待识别 %d 张（引擎：%s）" % (
                len(rows), "多模态 " + (cfg.get("model") or cfg.get("type"))
                if vision.configured(cfg) else "本地 Vision OCR"))
            done = failed = 0
            for index, row in enumerate(rows, 1):
                try:
                    row = app.media.download(row)
                    floor_row = app.db._one(
                        "SELECT content FROM floors WHERE tid=? AND floor=? AND sub_id=?",
                        (tid, row.get("floor") or 0, row.get("sub_id") or 0))
                    context = "%s ｜ #%s %s" % (
                        thread.get("title") or "", row.get("floor"),
                        util.clip((floor_row or {}).get("content") or "", 200))
                    out = vision.analyze_media(app.media, row, cfg, context=context)
                    analysis = out.get("analysis") or {}
                    job.log("[%d/%d] #%s %s：%s" % (
                        index, len(rows), row.get("floor"),
                        analysis.get("type") or "?",
                        util.clip((analysis.get("ocr_text") or "").replace("\n", " "), 60)))
                    done += 1
                except Exception as exc:
                    failed += 1
                    app.db.update_media(int(row["id"]), status="error",
                                        error=str(exc)[:300])
                    job.log("[%d/%d] 失败：%s" % (index, len(rows), exc))
            app.db.add_event("vision", "识图完成：%d 张成功 / %d 失败" % (done, failed),
                             tid=tid)
            return {"done": done, "failed": failed, "total": len(rows),
                    "engine": ("vlm" if vision.configured(cfg) else "macos-vision")}

        job = app.jobs.submit("vision", work)
        self._json({"ok": True, "job_id": job.id})

    # ------------------------------------------------------------- 事件
    def _stories_build(self, body: dict) -> None:
        tids = body.get("tids")
        if isinstance(tids, list):
            tids = [util.safe_int(t) for t in tids if util.safe_int(t)]
        else:
            tids = None
        threshold = util.safe_float(body.get("threshold"), 0.55)
        stories = linker.build_stories(self.app.db, tids, threshold=threshold)
        self._json({"ok": True, "stories": stories})

    def _story_detail(self, story_id: str) -> None:
        story = self.app.db.get_story(story_id)
        if not story:
            return self._error(404, "事件不存在")
        story["timeline"] = linker.story_timeline(self.app.db, story_id, limit=120)
        story["stats"] = linker.story_stats(self.app.db, story_id)
        story["reports"] = self.app.db.list_reports(story_id=story_id, limit=10)
        self._json({"ok": True, "story": story})

    def _story_summarize(self, story_id: str, body: dict) -> None:
        mode = body.get("mode") or "daily"
        provider = body.get("provider") or "auto"
        neutral = bool(body.get("neutral"))
        extra = str(body.get("extra") or "")[:500]
        app = self.app
        if not app.db.get_story(story_id):
            raise ValueError("事件不存在")

        def work(job: Job):
            job.log("合并多贴材料…")
            report = summarize.generate_story(app.db, story_id, mode, provider,
                                              neutral, extra, progress=job.log)
            job.log("事件报告完成：%s" % report["title"])
            return {"report_id": report["id"], "title": report["title"],
                    "meta": report["meta"]}

        job = app.jobs.submit("story", work)
        self._json({"ok": True, "job_id": job.id})

    def _forum_crawl(self, body: dict) -> None:
        kw = (body.get("kw") or "").strip()
        if not kw:
            raise ValueError("缺少吧名")
        pages = min(10, max(1, util.safe_int(body.get("pages"), 2)))
        keyword = (body.get("keyword") or "").strip()
        min_replies = max(0, util.safe_int(body.get("min_replies"), 20))
        limit = min(10, max(1, util.safe_int(body.get("limit"), 3)))
        app = self.app

        def work(job: Job):
            client = app.monitor._client()
            found = []
            for pn in range(1, pages + 1):
                data = client.forum_threads(kw, pn=pn, rn=30)
                job.log("吧列表第 %d 页：%d 帖" % (pn, len(data.get("threads") or [])))
                for thread in data.get("threads") or []:
                    if keyword and keyword not in (thread.get("title") or ""):
                        continue
                    if thread.get("reply_num", 0) < min_replies:
                        continue
                    found.append(thread)
                time.sleep(0.4)
            found.sort(key=lambda t: -t.get("reply_num", 0))
            picked = found[:limit]
            job.log("命中 %d 帖，取前 %d 帖抓详情" % (len(found), len(picked)))
            imported = []
            for thread in picked:
                try:
                    bundle = client.fetch_thread_pb(thread["tid"])
                    client.hydrate_subs(thread["tid"], bundle["floors"],
                                        op_id=bundle["meta"].get("author_id") or 0,
                                        max_pids=20)
                    app.db.upsert_thread(bundle["meta"])
                    app.db.upsert_floors(thread["tid"], bundle["floors"])
                    app.db.refresh_thread_counts(thread["tid"])
                    app.media.register_thread(thread["tid"])
                    imported.append(thread["tid"])
                    job.log("已抓《%s》%d 楼" % (
                        util.clip(thread.get("title") or "", 24), len(bundle["floors"])))
                except Exception as exc:
                    job.log("抓取失败 %s：%s" % (thread.get("tid"), exc))
            stories = linker.build_stories(app.db, None)
            job.log("聚类得到 %d 个事件" % len(stories))
            return {"threads": imported, "stories": [s["id"] for s in stories]}

        job = app.jobs.submit("forum", work)
        self._json({"ok": True, "job_id": job.id})

    # ------------------------------------------------------------- 导入
    def _import(self, body: dict) -> None:
        path = os.path.abspath(str(body.get("path") or ""))
        if not path or not os.path.isfile(path):
            raise ValueError("文件不存在：%s" % path)
        rows = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        by_tid = {}
        for row in rows:
            tid = util.safe_int(row.get("tid"))
            if not tid:
                continue
            floor = util.safe_int(row.get("floor"))
            if not floor:
                continue
            by_tid.setdefault(tid, []).append({
                "floor": floor, "sub_id": 0,
                "user_id": util.safe_int(row.get("user_id")),
                "user": row.get("nickname") or row.get("user_name") or "匿名",
                "content": row.get("content") or "",
                "ts": int(time.mktime(time.strptime(row["date"], "%Y-%m-%d %H:%M")))
                if row.get("date") else 0,
                "is_op": bool(row.get("_is_op")),
                "likes": util.safe_int(row.get("likes")),
                "sub_count": 0,
            })
        imported = 0
        for tid, floors in by_tid.items():
            self.app.db.upsert_thread({
                "tid": tid, "title": "导入：tid %s" % tid, "author": "", "author_id": 0,
                "forum": "", "forum_id": 0, "create_ts": min(f["ts"] for f in floors) if floors else 0,
                "reply_num": len(floors),
            })
            imported += len(self.app.db.upsert_floors(tid, floors))
            self.app.db.refresh_thread_counts(tid)
            self.app.db.set_thread_scan(tid, True)
        self._json({"ok": True, "threads": len(by_tid), "floors": imported})


# --------------------------------------------------------------------- CLI
def seed(app: App) -> None:
    """塞两个演示源：一个正在直播的面基贴 + 贴吧热榜。"""
    database = app.db
    database.add_source("thread", "11154226751", title="直播一下和自称C8男的面基", interval=120)
    database.add_source("hot", "hot", title="贴吧热榜", interval=900)
    database.add_event("system", "已载入演示源，首次全量抓取需要十几秒")
    app.monitor.kick()


def setup_logging(verbose: bool = False) -> None:
    util.ensure_dir(DATA_DIR)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger("guatan")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        fh = logging.FileHandler(os.path.join(DATA_DIR, "guatan.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    logging.getLogger("guatan.http").setLevel(logging.WARNING)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="瓜田雷达 · 贴吧吃瓜监控台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=os.path.join(DATA_DIR, "guatan.db"))
    parser.add_argument("--no-monitor", action="store_true", help="只开网页，不自动轮询")
    parser.add_argument("--seed", action="store_true", help="写入演示源")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    app = App(args.db, enable_monitor=not args.no_monitor)
    if args.seed:
        seed(app)
    Handler.app = app
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    url = "http://%s:%d" % (args.host, args.port)
    print("\n🍉  瓜田雷达已启动  %s" % url)
    print("    数据文件：%s" % os.path.abspath(args.db))
    print("    停止：Ctrl+C\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收工。")
    finally:
        server.server_close()
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
