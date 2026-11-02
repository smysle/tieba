#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控引擎：盯帖 / 盯吧 / 盯热榜。

线程池 + 每个源自己的 next_due。发现新楼层就入库、跑关键词、记事件；
帖子里连续冒新楼时自动进入「直播加速」模式（15 秒一拉，持续 5 分钟）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from . import tieba, util
from .media import MediaStore

DEFAULT_KEYWORDS = [
    "反转", "打脸", "实锤", "锤", "破防", "笑死", "绷不住", "离谱", "逆天",
    "急了", "删帖", "封号", "锁帖", "道歉", "澄清", "回应", "证据", "截图",
    "律师函", "报警", "起诉", "网暴", "瓜", "就这", "典",
]

BURST_SECONDS = 300
BURST_INTERVAL = 15
MIN_INTERVAL = 30
MAX_BACKOFF = 900


class Monitor:
    def __init__(self, db, logger: Optional[logging.Logger] = None,
                 workers: int = 4) -> None:
        self.db = db
        self.log = logger or logging.getLogger("guatan.monitor")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._next: Dict[int, float] = {}
        self._inflight = set()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="poll")
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "media")
        self.media = MediaStore(db, root)
        self.thread: Optional[threading.Thread] = None
        self.started_at = 0.0
        self.last_error = ""

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self.thread.start()
        self.log.info("监控引擎启动")

    def stop(self) -> None:
        self._stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.pool.shutdown(wait=False)
        self.log.info("监控引擎停止")

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive() and not self._stop.is_set())

    # ---------------------------------------------------------------- 配置读取
    def _monitor_cfg(self) -> dict:
        cfg = self.db.get_config("monitor", {}) or {}
        cfg.setdefault("max_pages", 40)
        cfg.setdefault("keywords", DEFAULT_KEYWORDS)
        cfg.setdefault("sub_pids", 40)
        return cfg

    def _client(self) -> tieba.TiebaClient:
        tieba_cfg = self.db.get_config("tieba", {}) or {}
        return tieba.TiebaClient(
            cookie=tieba_cfg.get("cookie", ""),
            delay=float(tieba_cfg.get("delay", 0.35)),
            log=lambda msg: self.log.info(msg),
        )

    # ------------------------------------------------------------------ 调度
    def kick(self, source_id: Optional[int] = None) -> None:
        with self._lock:
            if source_id is None:
                self._next = {sid: 0 for sid in self._next}
            else:
                self._next[int(source_id)] = 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = time.time()
                for src in self.db.enabled_sources():
                    sid = int(src["id"])
                    due = self._next.get(sid)
                    if due is None:
                        due = now + (2 if src["kind"] == "thread" else 10)
                        self._next[sid] = due
                    if due > now:
                        continue
                    with self._lock:
                        if sid in self._inflight:
                            continue
                        self._inflight.add(sid)
                    self._next[sid] = now + 120  # 占位，成功后覆盖
                    self.pool.submit(self._run_source, src)
            except Exception as exc:  # 调度器自身不能死
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                self.log.exception("调度异常")
            self._stop.wait(1.5)

    def _run_source(self, src: dict) -> None:
        sid = int(src["id"])
        started = time.time()
        try:
            if src["kind"] == "thread":
                new_count, total = self._poll_thread(src)
            elif src["kind"] == "forum":
                new_count, total = self._poll_forum(src)
            else:
                new_count, total = self._poll_hot(src)
            ms = int((time.time() - started) * 1000)
            self.db.add_poll(sid, True, ms, new_count, total)
            self.db.update_source(sid, status="ok", last_poll=time.time(),
                                  last_ok=time.time(), last_error="")
            self._schedule_next(src, new_count)
            self.log.info("源 %s(%s) 完成：新增 %d，共 %d，%dms",
                          sid, src["kind"], new_count, total, ms)
        except Exception as exc:
            ms = int((time.time() - started) * 1000)
            message = str(exc) or type(exc).__name__
            self.last_error = message
            self.db.add_poll(sid, False, ms, 0, 0, note=message)
            current = self.db.get_source(sid) or src
            fails = int((current.get("meta") or {}).get("fails") or 0) + 1
            meta = dict(current.get("meta") or {})
            meta["fails"] = fails
            backoff = min(MAX_BACKOFF, max(60, int(src.get("interval") or 120) * (2 ** min(fails, 4))))
            self.db.update_source(sid, status="error", last_poll=time.time(),
                                  last_error=message[:300], meta=meta)
            with self._lock:
                self._next[sid] = time.time() + backoff
            if src.get("status") != "error":
                self.db.add_event("error", "监控失败：%s" % message,
                                  source_id=sid, tid=self._tid_of(src))
            self.log.warning("源 %s 失败（第 %d 次，%ds 后重试）：%s", sid, fails, backoff, message)
        finally:
            with self._lock:
                self._inflight.discard(sid)

    def _schedule_next(self, src: dict, new_count: int) -> None:
        sid = int(src["id"])
        current = self.db.get_source(sid) or src
        meta = dict(current.get("meta") or {})
        meta["fails"] = 0
        now = time.time()
        burst_until = float(meta.get("burst_until") or 0)
        if new_count:
            burst_until = max(burst_until, now + BURST_SECONDS)
        meta["burst_until"] = burst_until
        interval = int(src.get("interval") or 120)
        due = now + (BURST_INTERVAL if burst_until > now else interval)
        if burst_until and burst_until <= now:
            meta["burst_until"] = 0
        self.db.update_source(sid, meta=meta)
        with self._lock:
            self._next[sid] = due

    @staticmethod
    def _tid_of(src: dict) -> Optional[int]:
        if src.get("kind") == "thread":
            return util.safe_int(src.get("key")) or None
        return None

    # -------------------------------------------------------------- 盯帖流程
    def _poll_thread(self, src: dict):
        tid = util.safe_int(src["key"])
        if not tid:
            raise tieba.TiebaError("监控源的 tid 不合法")
        row = self.db.get_thread(tid)
        full_scanned = float((row or {}).get("full_scanned") or 0)
        deep = (not row) or (not full_scanned) or (time.time() - full_scanned > 6 * 3600)
        client = self._client()
        cfg = self._monitor_cfg()
        self.db.update_source(src["id"], status="fetching")

        if deep:
            bundle = client.fetch_thread_pb(tid, max_pages=int(cfg.get("max_pages") or 40))
            client.hydrate_subs(tid, bundle["floors"],
                                op_id=bundle["meta"].get("author_id") or 0,
                                max_pids=int(cfg.get("sub_pids") or 40),
                                log=self.log.info)
            floors = bundle["floors"]
            new_floors = self.db.upsert_floors(tid, floors)
            self.db.upsert_thread(bundle["meta"])
            if bundle.get("complete"):
                self.db.mark_missing_deleted(
                    tid, [(f["floor"], f["sub_id"]) for f in floors])
            self.db.set_thread_scan(tid, True)
            self.db.refresh_thread_counts(tid)
            self.media.register_thread(tid)
            self.db.update_source(src["id"], title=bundle["meta"]["title"])
            if new_floors:
                self.db.add_event(
                    "import",
                    "入库《%s》：%d 楼（%d 页）" % (bundle["meta"]["title"], len(floors), bundle["pages"]),
                    source_id=src["id"], tid=tid,
                    payload={"floors": len(floors), "pages": bundle["pages"]},
                )
            return 0, len(floors)

        last_floor = self._max_floor(tid)
        has_first = self.db._one(
            "SELECT 1 AS ok FROM floors WHERE tid=? AND floor=1 AND sub_id=0", (tid,))
        bundle = client.fetch_new(tid, last_floor, include_first=not has_first)
        client.hydrate_subs(tid, bundle["floors"],
                            op_id=bundle["meta"].get("author_id") or 0,
                            max_pids=int(cfg.get("sub_pids") or 8),
                            log=self.log.info)
        floors = bundle["floors"]
        new_floors = self.db.upsert_floors(tid, floors)
        meta = bundle["meta"]
        old_meta = row or {}
        self.db.upsert_thread(meta)
        self.db.refresh_thread_counts(tid)
        self.media.register_thread(tid)
        if meta.get("title") and meta["title"] != old_meta.get("title"):
            self.db.update_source(src["id"], title=meta["title"])
            self.db.add_event("title", "标题改了：《%s》→《%s》" % (old_meta.get("title"), meta["title"]),
                              source_id=src["id"], tid=tid)

        mains = [f for f in new_floors if not f.get("sub_id")]
        subs = [f for f in new_floors if f.get("sub_id")]
        if mains:
            numbers = [f["floor"] for f in mains]
            span = "#%d" % numbers[0] if len(numbers) == 1 else "#%d–#%d" % (numbers[0], numbers[-1])
            text = "《%s》新增 %d 楼（%s）" % (meta["title"], len(mains), span)
            if subs:
                text += "，%d 条楼中楼" % len(subs)
            self.db.add_event("new_floors", text, source_id=src["id"], tid=tid,
                              payload={"floors": numbers[:50], "subs": len(subs)})
            self._keyword_scan(src, tid, mains + subs)
            op_new = [f for f in mains if f.get("is_op")]
            if op_new:
                self.db.add_event(
                    "op", "楼主更新了：%s" % util.one_line(op_new[-1].get("content"), 80),
                    source_id=src["id"], tid=tid,
                    payload={"floor": op_new[-1].get("floor")})
        return len(new_floors), len(floors)

    def _max_floor(self, tid: int) -> int:
        row = self.db._one("SELECT MAX(floor) AS f FROM floors WHERE tid=? AND sub_id=0", (tid,))
        return int((row or {}).get("f") or 0)

    def _keyword_scan(self, src: dict, tid: int, floors: List[dict]) -> None:
        keywords = list((src.get("meta") or {}).get("keywords") or [])
        if not keywords:
            keywords = self._monitor_cfg().get("keywords") or DEFAULT_KEYWORDS
        hits = []
        for floor in floors:
            content = floor.get("content") or ""
            for kw in keywords:
                if kw in content:
                    hits.append((kw, floor))
                    break
        for kw, floor in hits[:5]:
            self.db.add_event(
                "keyword",
                "关键词「%s」命中 #%s @%s：%s" % (
                    kw, floor.get("floor"), floor.get("user") or "匿名",
                    util.clip(floor.get("content"), 90)),
                source_id=src["id"], tid=tid,
                payload={"keyword": kw, "floor": floor.get("floor")},
            )

    # -------------------------------------------------------------- 盯吧流程
    def _poll_forum(self, src: dict):
        kw = str(src["key"])
        cfg = self._monitor_cfg()
        client = self._client()
        data = client.forum_threads(kw, rn=30)
        meta = dict(src.get("meta") or {})
        seen = set(str(x) for x in (meta.get("seen_tids") or []))
        threads = data.get("threads") or []
        fresh = [t for t in threads if str(t["tid"]) not in seen]
        if seen and fresh:
            top = sorted(fresh, key=lambda t: -t["reply_num"])[:3]
            self.db.add_event(
                "forum",
                "吧「%s」冒出新帖 %d 个：%s" % (
                    data.get("forum") or kw, len(fresh),
                    "、".join("《%s》" % util.clip(t["title"], 22) for t in top)),
                source_id=src["id"], payload={"tids": [t["tid"] for t in fresh]})
        auto = []
        min_replies = util.safe_int(meta.get("min_replies"), 30)
        want = (meta.get("keyword") or "").strip()
        known = {s["key"] for s in self.db.list_sources() if s["kind"] == "thread"}
        # 只在「已经建立基线之后」自动转盯，且要显式开启 auto_watch
        auto_enabled = bool(meta.get("auto_watch"))
        for thread in (fresh if (seen and auto_enabled) else []):
            if str(thread["tid"]) in known:
                continue
            if want and want not in thread["title"]:
                continue
            if thread["reply_num"] < min_replies:
                continue
            auto.append(thread["tid"])
            known.add(str(thread["tid"]))
            self.db.add_source("thread", str(thread["tid"]), title=thread["title"],
                               note="由盯吧「%s」自动转入" % kw,
                               interval=int(src.get("interval") or 300))
            self.db.add_event(
                "auto_watch",
                "自动开盯《%s》（%d 回复）" % (util.clip(thread["title"], 30), thread["reply_num"]),
                source_id=src["id"], tid=thread["tid"])
            if len(auto) >= 3:
                break
        merged = list(dict.fromkeys([str(t["tid"]) for t in threads] + list(seen)))[:300]
        meta["seen_tids"] = merged
        self.db.update_source(src["id"], meta=meta, title="吧：%s" % (data.get("forum") or kw))
        return (len(fresh) if seen else 0), len(threads)

    # ------------------------------------------------------------- 热榜流程
    def _poll_hot(self, src: dict):
        client = self._client()
        topics = client.hot_topics(limit=20)
        meta = dict(src.get("meta") or {})
        seen = set(str(x) for x in (meta.get("seen") or []))
        fresh = [t for t in topics if t["tid"] not in seen]
        if seen and fresh:
            self.db.add_event(
                "hot",
                "热榜上新：%s" % "、".join(util.clip(t["title"], 18) for t in fresh[:3]),
                source_id=src["id"], payload={"topics": [t["title"] for t in fresh[:10]]})
        meta["seen"] = [t["tid"] for t in topics]
        meta["hot"] = topics[:20]
        self.db.update_source(src["id"], meta=meta, title="贴吧热榜")
        return (len(fresh) if seen else 0), len(topics)

    # ---------------------------------------------------------------- 状态
    def status(self) -> dict:
        with self._lock:
            inflight = sorted(self._inflight)
            nxt = {str(k): round(v - time.time(), 1) for k, v in self._next.items()}
        return {
            "running": self.running(),
            "started_at": self.started_at,
            "uptime": int(time.time() - self.started_at) if self.started_at else 0,
            "inflight": inflight,
            "next_in": nxt,
            "last_error": self.last_error,
        }
