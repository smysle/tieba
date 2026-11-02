#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 存储层：监控源、楼层、事件、报告、配置。

单连接 + 互斥锁，够用且不会踩 sqlite 的多线程坑（WAL 已开）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Dict, Iterable, List, Optional

from . import util

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    title       TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    interval    INTEGER DEFAULT 120,
    enabled     INTEGER DEFAULT 1,
    status      TEXT DEFAULT 'idle',
    last_poll   REAL DEFAULT 0,
    last_ok     REAL DEFAULT 0,
    last_error  TEXT DEFAULT '',
    created_at  REAL DEFAULT 0,
    meta        TEXT DEFAULT '{}',
    UNIQUE(kind, key)
);

CREATE TABLE IF NOT EXISTS threads (
    tid          INTEGER PRIMARY KEY,
    title        TEXT DEFAULT '',
    author       TEXT DEFAULT '',
    author_id    INTEGER DEFAULT 0,
    forum        TEXT DEFAULT '',
    forum_id     INTEGER DEFAULT 0,
    create_ts    INTEGER DEFAULT 0,
    reply_num    INTEGER DEFAULT 0,
    floor_count  INTEGER DEFAULT 0,
    participants INTEGER DEFAULT 0,
    first_seen   REAL DEFAULT 0,
    last_seen    REAL DEFAULT 0,
    full_scanned REAL DEFAULT 0,
    meta         TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS floors (
    tid        INTEGER NOT NULL,
    floor      INTEGER NOT NULL DEFAULT 0,
    sub_id     INTEGER NOT NULL DEFAULT 0,
    user_id    INTEGER DEFAULT 0,
    user_name  TEXT DEFAULT '',
    content    TEXT DEFAULT '',
    ts         INTEGER DEFAULT 0,
    is_op      INTEGER DEFAULT 0,
    likes      INTEGER DEFAULT 0,
    sub_count  INTEGER DEFAULT 0,
    images     INTEGER DEFAULT 0,
    first_seen REAL DEFAULT 0,
    last_seen  REAL DEFAULT 0,
    starred    INTEGER DEFAULT 0,
    deleted    INTEGER DEFAULT 0,
    PRIMARY KEY (tid, floor, sub_id)
);
CREATE INDEX IF NOT EXISTS idx_floors_tid_ts ON floors(tid, ts);
CREATE INDEX IF NOT EXISTS idx_floors_tid_floor ON floors(tid, floor);

CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  INTEGER,
    ts         REAL,
    ok         INTEGER DEFAULT 1,
    ms         INTEGER DEFAULT 0,
    new_floors INTEGER DEFAULT 0,
    total      INTEGER DEFAULT 0,
    note       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_polls_source ON polls(source_id, ts);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL,
    source_id INTEGER,
    tid       INTEGER,
    kind      TEXT,
    text      TEXT,
    payload   TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS reports (
    id         TEXT PRIMARY KEY,
    tid        INTEGER,
    mode       TEXT,
    provider   TEXT,
    model      TEXT,
    title      TEXT,
    created_at REAL,
    markdown   TEXT,
    meta       TEXT DEFAULT '{}',
    cover      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reports_tid ON reports(tid, created_at);

CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tid         INTEGER NOT NULL,
    floor       INTEGER DEFAULT 0,
    sub_id      INTEGER DEFAULT 0,
    pid         INTEGER DEFAULT 0,
    pic_id      TEXT DEFAULT '',
    url         TEXT DEFAULT '',
    origin_url  TEXT DEFAULT '',
    path        TEXT DEFAULT '',
    thumb       TEXT DEFAULT '',
    sha1        TEXT DEFAULT '',
    mime        TEXT DEFAULT '',
    width       INTEGER DEFAULT 0,
    height      INTEGER DEFAULT 0,
    bytes       INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'new',
    error       TEXT DEFAULT '',
    analysis    TEXT DEFAULT '{}',
    analyzed_at REAL DEFAULT 0,
    created_at  REAL DEFAULT 0,
    UNIQUE(tid, floor, sub_id, pic_id)
);
CREATE INDEX IF NOT EXISTS idx_media_tid ON media(tid, status);

CREATE TABLE IF NOT EXISTS stories (
    id          TEXT PRIMARY KEY,
    title       TEXT DEFAULT '',
    forum       TEXT DEFAULT '',
    status      TEXT DEFAULT 'auto',
    created_at  REAL DEFAULT 0,
    updated_at  REAL DEFAULT 0,
    meta        TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS story_threads (
    story_id  TEXT NOT NULL,
    tid       INTEGER NOT NULL,
    role      TEXT DEFAULT '',
    score     REAL DEFAULT 0,
    added_at  REAL DEFAULT 0,
    PRIMARY KEY (story_id, tid)
);

CREATE TABLE IF NOT EXISTS config (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Db:
    def __init__(self, path: str) -> None:
        util.ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self._ensure_column("floors", "pid", "INTEGER DEFAULT 0")
            self._ensure_column("floors", "media_json", "TEXT DEFAULT '[]'")
            self._ensure_column("reports", "scope", "TEXT DEFAULT 'thread'")
            self._ensure_column("reports", "story_id", "TEXT DEFAULT ''")
            self.conn.commit()

    # ------------------------------------------------------------ internals
    def _exec(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(args))
            self.conn.commit()
            return cur

    def _all(self, sql: str, args: Iterable = ()) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(sql, tuple(args)).fetchall()
        return [dict(row) for row in rows]

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = [row["name"] for row in self._all("PRAGMA table_info(%s)" % table)]
        if column not in cols:
            self.conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))

    def _one(self, sql: str, args: Iterable = ()) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -------------------------------------------------------------- sources
    def add_source(self, kind: str, key: str, title: str = "", note: str = "",
                   interval: int = 120, meta: Optional[dict] = None) -> dict:
        now = time.time()
        meta_json = util.json_dumps(meta or {})
        self._exec(
            "INSERT INTO sources (kind, key, title, note, interval, enabled, status,"
            " created_at, meta) VALUES (?,?,?,?,?,1,'idle',?,?)"
            " ON CONFLICT(kind, key) DO UPDATE SET title=excluded.title,"
            " note=excluded.note, interval=excluded.interval, enabled=1,"
            " meta=excluded.meta",
            (kind, str(key), title, note, util.parse_interval(interval), now, meta_json),
        )
        src = self._one("SELECT * FROM sources WHERE kind=? AND key=?", (kind, str(key)))
        return self._source_out(src)

    def get_source(self, source_id: int) -> Optional[dict]:
        return self._source_out(self._one("SELECT * FROM sources WHERE id=?", (source_id,)))

    def list_sources(self) -> List[dict]:
        rows = self._all("SELECT * FROM sources ORDER BY created_at ASC")
        return [self._source_out(r) for r in rows]

    def enabled_sources(self) -> List[dict]:
        rows = self._all("SELECT * FROM sources WHERE enabled=1 ORDER BY id ASC")
        return [self._source_out(r) for r in rows]

    def update_source(self, source_id: int, **fields) -> None:
        allowed = {"title", "note", "interval", "enabled", "status", "last_poll",
                   "last_ok", "last_error", "meta"}
        sets, args = [], []
        for name, value in fields.items():
            if name not in allowed:
                continue
            if name == "meta" and not isinstance(value, str):
                value = util.json_dumps(value)
            sets.append("%s=?" % name)
            args.append(value)
        if not sets:
            return
        args.append(source_id)
        self._exec("UPDATE sources SET %s WHERE id=?" % ", ".join(sets), args)

    def delete_source(self, source_id: int) -> None:
        self._exec("DELETE FROM sources WHERE id=?", (source_id,))

    @staticmethod
    def _source_out(row: Optional[dict]) -> Optional[dict]:
        if not row:
            return None
        row = dict(row)
        row["meta"] = util.json_loads(row.get("meta"), {}) or {}
        return row

    # -------------------------------------------------------------- threads
    def upsert_thread(self, meta: dict) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO threads (tid, title, author, author_id, forum, forum_id,"
            " create_ts, reply_num, first_seen, last_seen, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(tid) DO UPDATE SET title=excluded.title,"
            " author=excluded.author, author_id=excluded.author_id,"
            " forum=excluded.forum, forum_id=excluded.forum_id,"
            " create_ts=COALESCE(NULLIF(excluded.create_ts,0), threads.create_ts),"
            " reply_num=excluded.reply_num, last_seen=excluded.last_seen",
            (meta["tid"], meta.get("title", ""), meta.get("author", ""),
             meta.get("author_id", 0), meta.get("forum", ""), meta.get("forum_id", 0),
             meta.get("create_ts", 0), meta.get("reply_num", 0), now, now,
             util.json_dumps(meta.get("extra") or {})),
        )

    def get_thread(self, tid: int) -> Optional[dict]:
        row = self._one("SELECT * FROM threads WHERE tid=?", (tid,))
        if not row:
            return None
        row["meta"] = util.json_loads(row.get("meta"), {}) or {}
        return row

    def list_threads(self) -> List[dict]:
        rows = self._all(
            "SELECT t.*, (SELECT COUNT(*) FROM floors f WHERE f.tid=t.tid"
            " AND f.sub_id=0 AND f.deleted=0) AS floors_now,"
            " (SELECT COUNT(*) FROM floors f WHERE f.tid=t.tid AND f.sub_id>0"
            " AND f.deleted=0) AS subs_now"
            " FROM threads t ORDER BY t.last_seen DESC"
        )
        for row in rows:
            row["meta"] = util.json_loads(row.get("meta"), {}) or {}
        return rows

    def set_thread_scan(self, tid: int, full: bool = True) -> None:
        now = time.time()
        self._exec("UPDATE threads SET full_scanned=?, last_seen=? WHERE tid=?",
                   (now if full else 0, now, tid))

    def refresh_thread_counts(self, tid: int) -> None:
        row = self._one(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT user_id) AS p FROM floors"
            " WHERE tid=? AND sub_id=0 AND deleted=0", (tid,))
        if not row:
            return
        self._exec("UPDATE threads SET floor_count=?, participants=? WHERE tid=?",
                   (row.get("n") or 0, row.get("p") or 0, tid))
        first = self._one(
            "SELECT MIN(ts) AS ts FROM floors WHERE tid=? AND sub_id=0 AND ts>0", (tid,))
        if first and first.get("ts"):
            self._exec("UPDATE threads SET create_ts=? WHERE tid=?"
                       " AND (create_ts=0 OR create_ts>?)", (first["ts"], tid, first["ts"]))

    # --------------------------------------------------------------- floors
    def upsert_floors(self, tid: int, floors: List[dict]) -> List[dict]:
        """写入楼层，返回本次新增的楼层（用于事件与提醒）。"""
        now = time.time()
        new_rows: List[dict] = []
        with self._lock:
            cur = self.conn.cursor()
            for f in floors:
                floor_no = f.get("floor") or 0
                sub_id = f.get("sub_id") or 0
                cur.execute(
                    "INSERT OR IGNORE INTO floors (tid, floor, sub_id, user_id, user_name,"
                    " content, ts, is_op, likes, sub_count, images, pid, media_json,"
                    " first_seen, last_seen)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, floor_no, sub_id, f.get("user_id") or 0,
                     f.get("user") or "", f.get("content") or "", f.get("ts") or 0,
                     1 if f.get("is_op") else 0, f.get("likes") or 0,
                     f.get("sub_count") or 0, f.get("images") or 0,
                     f.get("pid") or 0, util.json_dumps(f.get("media") or []), now, now),
                )
                if cur.rowcount == 1:
                    new_rows.append(f)
                else:
                    cur.execute(
                        "UPDATE floors SET user_name=?, content=?, likes=MAX(likes, ?),"
                        " sub_count=MAX(sub_count, ?), pid=?, media_json=?, deleted=0,"
                        " last_seen=? WHERE tid=? AND floor=? AND sub_id=?",
                        (f.get("user") or "", f.get("content") or "",
                         f.get("likes") or 0, f.get("sub_count") or 0,
                         f.get("pid") or 0, util.json_dumps(f.get("media") or []), now,
                         tid, floor_no, sub_id),
                    )
            self.conn.commit()
        return new_rows

    def mark_missing_deleted(self, tid: int, seen_pairs: Iterable,
                             include_subs: bool = False) -> int:
        """全量扫描后，把服务端已经不返回的楼层标记为删除。

        默认只对主楼层做对账：楼中楼是分批补的，不全量抓完就标记会误伤。
        """
        seen = {(int(a), int(b)) for a, b in seen_pairs}
        sql = "SELECT floor, sub_id FROM floors WHERE tid=? AND deleted=0"
        if not include_subs:
            sql += " AND sub_id=0"
        rows = self._all(sql, (tid,))
        dead = [(r["floor"], r["sub_id"]) for r in rows if (r["floor"], r["sub_id"]) not in seen]
        if dead:
            with self._lock:
                self.conn.executemany(
                    "UPDATE floors SET deleted=1 WHERE tid=? AND floor=? AND sub_id=?",
                    [(tid, a, b) for a, b in dead])
                self.conn.commit()
        return len(dead)

    def list_floor_rows(self, tid: int, include_subs: bool = True) -> List[dict]:
        sql = "SELECT * FROM floors WHERE tid=? AND deleted=0"
        if not include_subs:
            sql += " AND sub_id=0"
        sql += " ORDER BY floor ASC, sub_id ASC"
        return self._all(sql, (tid,))

    def grouped_floors(self, tid: int, limit: int = 0, offset: int = 0,
                       mode: str = "all", query: str = "") -> List[dict]:
        """主楼层 + 楼中楼挂载，按楼层升序。"""
        rows = self.list_floor_rows(tid)
        mains, subs = [], {}
        for row in rows:
            row["starred"] = bool(row.get("starred"))
            if row.get("sub_id"):
                subs.setdefault(row["floor"], []).append(row)
            else:
                mains.append(row)
        for floor in mains:
            floor["subs"] = subs.get(floor["floor"], [])

        query = (query or "").strip()
        if query:
            mains = [f for f in mains
                     if query in (f.get("content") or "") or query in (f.get("user_name") or "")]

        if mode == "op":
            mains = [f for f in mains if f.get("is_op")]
        elif mode == "starred":
            mains = [f for f in mains if f.get("starred")]
        elif mode == "hot":
            mains = [f for f in mains
                     if (f.get("likes") or 0) >= 1 or (f.get("sub_count") or 0) >= 3
                     or len(f.get("content") or "") >= 60]
            mains.sort(key=lambda f: -((f.get("likes") or 0) * 3
                                       + (f.get("sub_count") or 0) * 2
                                       + min(len(f.get("content") or ""), 200) / 20))
        if limit:
            mains = mains[offset:offset + limit]
        return mains

    def set_star(self, tid: int, floor: int, sub_id: int, starred: bool) -> None:
        self._exec("UPDATE floors SET starred=? WHERE tid=? AND floor=? AND sub_id=?",
                   (1 if starred else 0, tid, floor, sub_id))

    def thread_stats(self, tid: int) -> dict:
        rows = self._all(
            "SELECT floor, user_id, user_name, ts, is_op, content, likes, sub_count"
            " FROM floors WHERE tid=? AND sub_id=0 AND deleted=0", (tid,))
        def hour_key(ts):
            return time.strftime("%m-%d %H:00", time.localtime(ts))

        buckets: Dict[str, int] = {}
        users: Dict[str, int] = {}
        first_ts = last_ts = 0
        op_floors = 0
        for row in rows:
            ts = row.get("ts") or 0
            if ts:
                buckets[hour_key(ts)] = buckets.get(hour_key(ts), 0) + 1
                first_ts = min(first_ts or ts, ts)
                last_ts = max(last_ts, ts)
            key = str(row.get("user_id") or row.get("user_name") or "匿名")
            users[key] = users.get(key, 0) + 1
            if row.get("is_op"):
                op_floors += 1
        series = [{"label": k, "count": buckets[k]} for k in sorted(buckets)]
        top_users = sorted(users.items(), key=lambda kv: -kv[1])[:8]
        return {
            "floors": len(rows),
            "participants": len(users),
            "op_floors": op_floors,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "span": (last_ts - first_ts) if first_ts and last_ts else 0,
            "series": series,
            "top_users": [{"uid": k, "count": v} for k, v in top_users],
        }

    def delete_thread(self, tid: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM floors WHERE tid=?", (tid,))
            self.conn.execute("DELETE FROM reports WHERE tid=?", (tid,))
            self.conn.execute("DELETE FROM media WHERE tid=?", (tid,))
            self.conn.execute("DELETE FROM events WHERE tid=?", (tid,))
            self.conn.execute("DELETE FROM sources WHERE kind='thread' AND key=?", (str(tid),))
            self.conn.execute("DELETE FROM threads WHERE tid=?", (tid,))
            self.conn.commit()

    def subscribed_tids(self) -> List[int]:
        return [int(s["key"]) for s in self.enabled_sources() if s["kind"] == "thread"]

    # ---------------------------------------------------------------- polls
    def add_poll(self, source_id: Optional[int], ok: bool, ms: int,
                 new_floors: int, total: int, note: str = "") -> None:
        self._exec(
            "INSERT INTO polls (source_id, ts, ok, ms, new_floors, total, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (source_id, time.time(), 1 if ok else 0, ms, new_floors, total, note[:300]),
        )

    def poll_series(self, source_id: int, limit: int = 60) -> List[dict]:
        rows = self._all(
            "SELECT ts, ok, new_floors, total FROM polls WHERE source_id=?"
            " ORDER BY ts DESC LIMIT ?", (source_id, limit))
        return list(reversed(rows))

    def latest_poll(self, source_id: int) -> Optional[dict]:
        return self._one("SELECT * FROM polls WHERE source_id=? ORDER BY ts DESC LIMIT 1",
                         (source_id,))

    # --------------------------------------------------------------- events
    def add_event(self, kind: str, text: str, source_id: Optional[int] = None,
                  tid: Optional[int] = None, payload: Optional[dict] = None) -> None:
        self._exec(
            "INSERT INTO events (ts, source_id, tid, kind, text, payload)"
            " VALUES (?,?,?,?,?,?)",
            (time.time(), source_id, tid, kind, text[:500], util.json_dumps(payload or {})),
        )

    def list_events(self, limit: int = 80, tid: Optional[int] = None) -> List[dict]:
        if tid:
            rows = self._all("SELECT * FROM events WHERE tid=? ORDER BY id DESC LIMIT ?",
                             (tid, limit))
        else:
            rows = self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = util.json_loads(row.get("payload"), {}) or {}
        return rows

    def unread_events(self, since_id: int) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM events WHERE id>?", (since_id,))
        return int((row or {}).get("n") or 0)

    # --------------------------------------------------------------- reports
    def save_report(self, report: dict) -> str:
        rid = report.get("id") or uuid.uuid4().hex[:12]
        self._exec(
            "INSERT OR REPLACE INTO reports (id, tid, mode, provider, model, title,"
            " created_at, markdown, meta, cover, scope, story_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, report.get("tid"), report.get("mode"), report.get("provider"),
             report.get("model", ""), report.get("title", ""), time.time(),
             report.get("markdown", ""), util.json_dumps(report.get("meta") or {}),
             report.get("cover", ""), report.get("scope", "thread"),
             report.get("story_id", "")),
        )
        return rid

    def get_report(self, report_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM reports WHERE id=?", (report_id,))
        if not row:
            return None
        row["meta"] = util.json_loads(row.get("meta"), {}) or {}
        return row

    def list_reports(self, tid: Optional[int] = None, limit: int = 20,
                     story_id: Optional[str] = None, scope: Optional[str] = None) -> List[dict]:
        if story_id:
            rows = self._all(
                "SELECT id, tid, mode, provider, model, title, created_at, scope, story_id,"
                " length(markdown) AS chars FROM reports WHERE story_id=?"
                " ORDER BY created_at DESC LIMIT ?", (story_id, limit))
            return rows
        if scope:
            rows = self._all(
                "SELECT id, tid, mode, provider, model, title, created_at, scope, story_id,"
                " length(markdown) AS chars FROM reports WHERE scope=?"
                " ORDER BY created_at DESC LIMIT ?", (scope, limit))
            return rows
        if tid:
            rows = self._all(
                "SELECT id, tid, mode, provider, model, title, created_at,"
                " length(markdown) AS chars FROM reports WHERE tid=?"
                " ORDER BY created_at DESC LIMIT ?", (tid, limit))
        else:
            rows = self._all(
                "SELECT id, tid, mode, provider, model, title, created_at, scope,"
                " story_id, length(markdown) AS chars FROM reports"
                " ORDER BY created_at DESC LIMIT ?", (limit,))
        return rows

    # ----------------------------------------------------------------- media
    def add_media(self, rows: List[dict]) -> int:
        now = time.time()
        inserted = 0
        with self._lock:
            cur = self.conn.cursor()
            for row in rows:
                cur.execute(
                    "INSERT OR IGNORE INTO media (tid, floor, sub_id, pid, pic_id, url,"
                    " origin_url, width, height, bytes, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (row["tid"], row.get("floor") or 0, row.get("sub_id") or 0,
                     row.get("pid") or 0, str(row.get("pic_id") or ""),
                     row.get("url") or "", row.get("origin_url") or "",
                     row.get("width") or 0, row.get("height") or 0,
                     row.get("bytes") or 0, now),
                )
                inserted += 1 if cur.rowcount == 1 else 0
            self.conn.commit()
        return inserted

    def media_rows(self, tid: Optional[int] = None, status: Optional[str] = None,
                   limit: int = 500) -> List[dict]:
        sql = "SELECT * FROM media WHERE 1=1"
        args: List = []
        if tid:
            sql += " AND tid=?"
            args.append(tid)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(limit)
        rows = self._all(sql, args)
        for row in rows:
            row["analysis"] = util.json_loads(row.get("analysis"), {}) or {}
        return rows

    def get_media(self, media_id: int) -> Optional[dict]:
        row = self._one("SELECT * FROM media WHERE id=?", (media_id,))
        if row:
            row["analysis"] = util.json_loads(row.get("analysis"), {}) or {}
        return row

    def update_media(self, media_id: int, **fields) -> None:
        allowed = {"path", "thumb", "sha1", "mime", "width", "height", "bytes",
                   "status", "error", "analysis", "analyzed_at", "url", "origin_url"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "analysis" and not isinstance(value, str):
                value = util.json_dumps(value)
            sets.append("%s=?" % key)
            args.append(value)
        if not sets:
            return
        args.append(media_id)
        self._exec("UPDATE media SET %s WHERE id=?" % ", ".join(sets), args)

    def media_count(self, tid: int) -> dict:
        row = self._one(
            "SELECT COUNT(*) AS n, SUM(status='done') AS ok, SUM(status='error') AS bad"
            " FROM media WHERE tid=?", (tid,))
        return {"total": int((row or {}).get("n") or 0),
                "done": int((row or {}).get("ok") or 0),
                "error": int((row or {}).get("bad") or 0)}

    def floors_with_media(self, tid: int) -> List[dict]:
        rows = self._all("SELECT * FROM floors WHERE tid=? AND deleted=0"
                         " AND media_json NOT IN ('[]','') ORDER BY floor", (tid,))
        return rows

    # ---------------------------------------------------------------- stories
    def save_story(self, story: dict, threads: List[dict]) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO stories (id, title, forum, status, created_at, updated_at, meta)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET title=excluded.title, forum=excluded.forum,"
            " status=excluded.status, updated_at=excluded.updated_at, meta=excluded.meta",
            (story["id"], story.get("title", ""), story.get("forum", ""),
             story.get("status", "auto"), now, now, util.json_dumps(story.get("meta") or {})),
        )
        with self._lock:
            self.conn.execute("DELETE FROM story_threads WHERE story_id=?", (story["id"],))
            self.conn.executemany(
                "INSERT OR REPLACE INTO story_threads (story_id, tid, role, score, added_at)"
                " VALUES (?,?,?,?,?)",
                [(story["id"], t["tid"], t.get("role", ""), t.get("score", 0.0), now)
                 for t in threads],
            )
            self.conn.commit()

    def list_stories(self) -> List[dict]:
        rows = self._all(
            "SELECT s.*, (SELECT COUNT(*) FROM story_threads st WHERE st.story_id=s.id)"
            " AS thread_count FROM stories s ORDER BY s.updated_at DESC")
        for row in rows:
            row["meta"] = util.json_loads(row.get("meta"), {}) or {}
        return rows

    def get_story(self, story_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM stories WHERE id=?", (story_id,))
        if not row:
            return None
        row["meta"] = util.json_loads(row.get("meta"), {}) or {}
        row["threads"] = self.story_threads(story_id)
        return row

    def story_threads(self, story_id: str) -> List[dict]:
        rows = self._all(
            "SELECT st.tid, st.role, st.score, t.title, t.author, t.forum,"
            " t.create_ts, t.reply_num, t.floor_count"
            " FROM story_threads st LEFT JOIN threads t ON t.tid=st.tid"
            " WHERE st.story_id=? ORDER BY st.rowid ASC", (story_id,))
        return rows

    def delete_auto_stories(self) -> None:
        """只清聚类结果，保留已生成的事件报告。"""
        with self._lock:
            self.conn.execute("DELETE FROM stories WHERE status='auto'")
            self.conn.execute("DELETE FROM story_threads WHERE story_id NOT IN"
                              " (SELECT id FROM stories)")
            self.conn.commit()

    def delete_story(self, story_id: str) -> None:
        self._exec("DELETE FROM stories WHERE id=?", (story_id,))
        self._exec("DELETE FROM story_threads WHERE story_id=?", (story_id,))
        self._exec("DELETE FROM reports WHERE story_id=?", (story_id,))

    # ---------------------------------------------------------------- config
    def set_config(self, key: str, value) -> None:
        self._exec("INSERT OR REPLACE INTO config (k, v) VALUES (?,?)",
                   (key, util.json_dumps(value)))

    def get_config(self, key: str, default=None):
        row = self._one("SELECT v FROM config WHERE k=?", (key,))
        if not row:
            return default
        return util.json_loads(row.get("v"), default)

    def config_all(self) -> dict:
        rows = self._all("SELECT k, v FROM config")
        return {r["k"]: util.json_loads(r.get("v")) for r in rows}
