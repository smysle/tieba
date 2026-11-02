#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""贴吧接口客户端：只看帖子，不做登录态以外的任何操作。

接口细节沿用本项目 `docs/tieba-api.md` 的逆向结论：
  sign = md5(按 key 排序拼接 + PC 盐)
  page_pc: r=1 倒序可匿名翻到底，r=2 正序匿名只给第一页
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from typing import Dict, List, Optional, Tuple

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PC_SALT = "36770b1f34c9bbf2e7d1a99d2b82fa9e"
BASE = "https://tieba.baidu.com"
BLOCK_MARKERS = ("百度安全验证", "wappass.baidu.com", "请开启JavaScript并刷新此页面")


class TiebaError(RuntimeError):
    """抓取失败，带人话提示。"""


def sign_params(params: Dict[str, str], salt: str = PC_SALT) -> str:
    raw = "".join("%s=%s" % (k, params[k]) for k in sorted(params)) + salt
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _content_text(content) -> str:
    if isinstance(content, str):
        return _clean(content)
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for seg in content:
        if isinstance(seg, str):
            parts.append(seg)
            continue
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        if kind in (0, 4):
            parts.append(str(seg.get("text") or ""))
        elif kind == 2:
            parts.append(str(seg.get("c") or ""))
        elif kind == 3:
            parts.append("[图片]")
        elif kind == 10:
            parts.append("[语音]")
    return _clean("".join(parts))


def _clean(text: str) -> str:
    text = str(text or "")
    for tag in ("<br/>", "<br />", "<br>", "<BR/>", "<BR>"):
        text = text.replace(tag, "\n")
    text = re.sub(r"<[^>]{1,200}>", "", text)
    return unescape(text).strip()


def _content_media(content) -> list:
    """把 content 里的图片段解析成可下载的 media 列表。"""
    media = []
    if not isinstance(content, list):
        return media
    for seg in content:
        if not isinstance(seg, dict) or seg.get("type") != 3:
            continue
        url = seg.get("origin_src") or seg.get("big_cdn_src") or seg.get("cdn_src") or ""
        if not url:
            continue
        size = str(seg.get("bsize") or "")
        width = height = 0
        if "," in size:
            try:
                width, height = [int(x) for x in size.split(",")[:2]]
            except ValueError:
                width = height = 0
        media.append({
            "pic_id": str(seg.get("pic_id") or ""),
            "url": url,
            "origin_url": seg.get("origin_src") or "",
            "width": width,
            "height": height,
            "is_long": bool(seg.get("is_long_pic")),
        })
    return media


def _count_images(content) -> int:
    if not isinstance(content, list):
        return 0
    return sum(1 for seg in content if isinstance(seg, dict) and seg.get("type") == 3)


def fmt_time(ts) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


class TiebaClient:
    def __init__(self, cookie: str = "", timeout: float = 20.0,
                 delay: float = 0.35, log=None) -> None:
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        self.delay = max(0.0, delay)
        self.log = log or (lambda *_: None)
        self._tbs = ""
        self._tbs_ts = 0.0

    # ------------------------------------------------------------- 基础请求
    def headers(self, tid: int = 0) -> Dict[str, str]:
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "x-requested-with": "XMLHttpRequest",
        }
        if tid:
            headers["Referer"] = "%s/p/%d?fr=frs" % (BASE, tid)
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _open(self, req, timeout: Optional[float] = None):
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 412):
                raise TiebaError("百度安全验证（HTTP %d）：换网络或配置 BDUSS 后再试" % exc.code)
            raise TiebaError("HTTP %d：%s" % (exc.code, exc.reason))
        except urllib.error.URLError as exc:
            raise TiebaError("网络不可用：%s" % (exc.reason,))
        except TimeoutError:
            raise TiebaError("请求超时")

    def _json(self, req, timeout: Optional[float] = None) -> dict:
        raw = self._open(req, timeout)
        text = raw.decode("utf-8", "replace")
        if text[:1] != "{" and any(m in text for m in BLOCK_MARKERS):
            raise TiebaError("被百度安全验证拦了，换个网络或带 BDUSS cookie")
        try:
            return json.loads(text)
        except ValueError:
            raise TiebaError("接口返回的不是 JSON：%s" % text[:120])

    def sync_tbs(self, force: bool = False) -> str:
        """tbs 会过期，批量抓之前刷新一次；110001 时再刷。"""
        if not force and self._tbs and time.time() - self._tbs_ts < 240:
            return self._tbs
        params = {"subapp_type": "pc", "_client_type": "20"}
        params["sign"] = sign_params(params)
        url = BASE + "/c/s/pc/sync?" + urllib.parse.urlencode(params)
        payload = self._json(urllib.request.Request(url, headers=self.headers()))
        tbs = ((payload.get("data") or {}).get("anti") or {}).get("tbs")
        if not tbs:
            raise TiebaError("拿不到 tbs：%s" % json.dumps(payload, ensure_ascii=False)[:160])
        self._tbs, self._tbs_ts = tbs, time.time()
        return tbs

    def page(self, tid: int, pn: int = 1, r: int = 1) -> dict:
        tbs = self.sync_tbs()
        for attempt in (1, 2):
            params = {
                "pn": str(pn), "lz": "0", "r": str(r), "mark_type": "0", "back": "0",
                "fr": "frs", "kz": str(tid), "session_request_times": "1",
                "tbs": tbs, "subapp_type": "pc", "_client_type": "20",
            }
            params["sign"] = sign_params(params)
            headers = self.headers(tid)
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
            req = urllib.request.Request(
                BASE + "/c/f/pb/page_pc",
                data=urllib.parse.urlencode(params).encode("utf-8"),
                headers=headers,
            )
            payload = self._json(req)
            code = _int(payload.get("error_code"))
            if code == 110001 and attempt == 1:
                tbs = self.sync_tbs(force=True)
                continue
            if code:
                raise TiebaError("page_pc 报错 %s：%s"
                                 % (code, payload.get("error_msg") or "未知"))
            return payload
        raise TiebaError("page_pc 连续失败")

    # ------------------------------------------------------------- 帖子数据
    @staticmethod
    def _floor_from_post(post: dict, users: Dict[int, dict], op_id: int, tid: int,
                         parent_floor: int = 0, parent_pid: int = 0) -> dict:
        author = post.get("author") or {}
        uid = _int(post.get("author_id")) or _int(author.get("id"))
        user = users.get(uid) or {}
        name = (author.get("name_show") or author.get("name")
                or user.get("name_show") or user.get("name") or "").strip()
        agree = post.get("agree") or {}
        media = _content_media(post.get("content"))
        return {
            "floor": _int(post.get("floor")) if not parent_floor else parent_floor,
            "sub_id": _int(post.get("id")) if parent_floor else 0,
            "pid": parent_pid if parent_floor else _int(post.get("id")),
            "user_id": uid,
            "user": name or "匿名",
            "content": _content_text(post.get("content")),
            "ts": _int(post.get("time")),
            "is_op": bool(op_id and uid == op_id),
            "likes": _int(agree.get("agree_num")),
            "sub_count": _int(post.get("sub_post_number")),
            "images": len(media) or _count_images(post.get("content")),
            "media": media,
        }

    def _absorb(self, payload: dict, tid: int, users: Dict[int, dict],
                op_id: int, floors: Dict[Tuple[int, int], dict]) -> None:
        for user in payload.get("user_list") or []:
            uid = _int(user.get("id"))
            if uid:
                users[uid] = {
                    "name": (user.get("name") or "").strip(),
                    "name_show": (user.get("name_show") or "").strip(),
                }
        first_floor = payload.get("first_floor")
        if isinstance(first_floor, dict) and _int(first_floor.get("floor")):
            row = self._floor_from_post(first_floor, users, op_id, tid)
            if row["floor"]:
                floors[(row["floor"], 0)] = row
        for post in payload.get("post_list") or []:
            main = self._floor_from_post(post, users, op_id, tid)
            if main["floor"]:
                floors[(main["floor"], 0)] = main
            subs = ((post.get("sub_post_list") or {}).get("sub_post_list") or [])
            for sub in subs:
                row = self._floor_from_post(sub, users, op_id, tid,
                                            parent_floor=main["floor"] or 0,
                                            parent_pid=main.get("pid") or 0)
                if row["sub_id"] and row["floor"]:
                    floors[(row["floor"], row["sub_id"])] = row

    def thread_meta(self, payload: dict, tid: int) -> dict:
        thread = payload.get("thread") or {}
        author = thread.get("author") or {}
        forum = payload.get("forum") or {}
        return {
            "tid": tid,
            "title": (thread.get("title") or "").strip() or "帖子 %d" % tid,
            "author": (author.get("name_show") or author.get("name") or "").strip(),
            "author_id": _int(author.get("id")),
            "forum": (forum.get("name") or "").strip(),
            "forum_id": _int(forum.get("id")),
            "create_ts": _int(thread.get("create_time")),
            "reply_num": _int(thread.get("reply_num")),
        }

    def fetch_thread(self, tid: int, max_pages: int = 40,
                     on_page=None) -> dict:
        """全量抓取（用于首次入库），r=1 倒序翻页。"""
        first = self.page(tid, 1)
        meta = self.thread_meta(first, tid)
        op_id = meta["author_id"]
        users: Dict[int, dict] = {}
        floors: Dict[Tuple[int, int], dict] = {}
        self._absorb(first, tid, users, op_id, floors)
        # r=1 倒序拿不到 1 楼（楼主正文），补一次 r=2 第一页（匿名可用）
        try:
            self._absorb(self.page(tid, 1, r=2), tid, users, op_id, floors)
        except TiebaError as exc:
            self.log("补抓 1 楼失败：%s" % exc)
        total_pages = _int((first.get("page") or {}).get("total_page")) or 1
        if on_page:
            on_page(1, total_pages, len(floors))

        pages = 1
        for pn in range(2, min(total_pages, max_pages) + 1):
            try:
                payload = self.page(tid, pn)
            except TiebaError as exc:
                self.log("第 %d 页失败：%s" % (pn, exc))
                break
            before = len(floors)
            self._absorb(payload, tid, users, op_id, floors)
            pages = pn
            if on_page:
                on_page(pn, total_pages, len(floors))
            if len(floors) == before:  # 末尾空页
                break
            time.sleep(self.delay)
        rows = sorted(floors.values(), key=lambda f: (f["floor"], f["sub_id"]))
        return {
            "meta": meta,
            "floors": rows,
            "pages": pages,
            "total_pages": total_pages,
            "complete": pages >= min(total_pages, max_pages),
        }

    def fetch_new(self, tid: int, last_floor: int, max_pages: int = 6,
                  include_first: bool = False) -> dict:
        """增量抓取：从最新一页往回翻，直到接上已知楼层。"""
        first = self.page(tid, 1)
        meta = self.thread_meta(first, tid)
        op_id = meta["author_id"]
        users: Dict[int, dict] = {}
        floors: Dict[Tuple[int, int], dict] = {}
        self._absorb(first, tid, users, op_id, floors)
        if include_first:
            try:
                self._absorb(self.page(tid, 1, r=2), tid, users, op_id, floors)
            except TiebaError as exc:
                self.log("补抓 1 楼失败：%s" % exc)
        pages = 1
        lowest = min((f for f, s in floors if s == 0), default=0)
        while last_floor and lowest and lowest > last_floor and pages < max_pages:
            pages += 1
            payload = self.page(tid, pages)
            before = len(floors)
            self._absorb(payload, tid, users, op_id, floors)
            if len(floors) == before:
                break
            lowest = min((f for f, s in floors if s == 0), default=lowest)
            time.sleep(self.delay)
        rows = sorted(floors.values(), key=lambda f: (f["floor"], f["sub_id"]))
        return {"meta": meta, "floors": rows, "pages": pages}

    # ------------------------------------------------------- pb/page 系列
    def page_pb(self, tid: int, pn: int = 1, rn: int = 30, r: int = 1,
                lz: int = 0) -> dict:
        """客户端风格接口：实测每页 30 楼，r=1 为正序（第 1 页是 1 楼起）。"""
        tbs = self.sync_tbs()
        for attempt in (1, 2):
            params = {
                "kz": str(tid), "pn": str(pn), "rn": str(rn), "r": str(r),
                "lz": str(lz), "tbs": tbs, "subapp_type": "pc", "_client_type": "20",
            }
            params["sign"] = sign_params(params)
            headers = self.headers(tid)
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
            req = urllib.request.Request(
                BASE + "/c/f/pb/page",
                data=urllib.parse.urlencode(params).encode("utf-8"),
                headers=headers,
            )
            payload = self._json(req)
            code = _int(payload.get("error_code"))
            if code == 110001 and attempt == 1:
                tbs = self.sync_tbs(force=True)
                continue
            if code:
                raise TiebaError("pb/page 报错 %s：%s"
                                 % (code, payload.get("error_msg") or "未知"))
            return payload
        raise TiebaError("pb/page 连续失败")

    def fetch_thread_pb(self, tid: int, max_pages: int = 60) -> dict:
        """正序全量：30 楼/页，第 1 页含 1 楼正文。"""
        first = self.page_pb(tid, 1)
        meta = self.thread_meta(first, tid)
        op_id = meta["author_id"]
        users: Dict[int, dict] = {}
        floors: Dict[Tuple[int, int], dict] = {}
        self._absorb(first, tid, users, op_id, floors)
        total_pages = _int((first.get("page") or {}).get("total_page")) or 1
        pages = 1
        for pn in range(2, min(total_pages, max_pages) + 1):
            try:
                payload = self.page_pb(tid, pn)
            except TiebaError as exc:
                self.log("pb/page 第 %d 页失败：%s" % (pn, exc))
                break
            before = len(floors)
            self._absorb(payload, tid, users, op_id, floors)
            pages = pn
            if len(floors) == before:
                break
            time.sleep(self.delay)
        rows = sorted(floors.values(), key=lambda f: (f["floor"], f["sub_id"]))
        return {
            "meta": meta, "floors": rows, "pages": pages,
            "total_pages": total_pages,
            "complete": pages >= min(total_pages, max_pages),
        }

    def floor_subs(self, tid: int, pid: int, parent_floor: int,
                   op_id: int = 0, rn: int = 50, max_pages: int = 8) -> List[dict]:
        """楼中楼专用接口，返回完整 subpost 列表。"""
        if not pid:
            return []
        tbs = self.sync_tbs()
        out: Dict[int, dict] = {}
        for pn in range(1, max_pages + 1):
            params = {"kz": str(tid), "pid": str(pid), "pn": str(pn), "rn": str(rn),
                      "tbs": tbs, "subapp_type": "pc", "_client_type": "20"}
            params["sign"] = sign_params(params)
            headers = self.headers(tid)
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
            req = urllib.request.Request(
                BASE + "/c/f/pb/floor",
                data=urllib.parse.urlencode(params).encode("utf-8"),
                headers=headers,
            )
            payload = self._json(req)
            code = _int(payload.get("error_code"))
            if code == 110001:
                tbs = self.sync_tbs(force=True)
                params["tbs"] = tbs
                continue
            if code:
                raise TiebaError("floor 报错 %s：%s" % (code, payload.get("error_msg")))
            items = payload.get("subpost_list") or []
            if not items:
                break
            users: Dict[int, dict] = {}
            for item in items:
                row = self._floor_from_post(item, users, op_id, tid,
                                            parent_floor=parent_floor, parent_pid=pid)
                if row["sub_id"]:
                    out[row["sub_id"]] = row
            total = _int(payload.get("subpost_num"))
            if total and len(out) >= total:
                break
            time.sleep(self.delay * 0.6)
        return sorted(out.values(), key=lambda r: r["ts"])

    def hydrate_subs(self, tid: int, floors: List[dict], op_id: int = 0,
                     max_pids: int = 60, log=None) -> int:
        """给主楼层补齐楼中楼（只补内嵌数量不足的）。"""
        mains = [f for f in floors if not f.get("sub_id") and (f.get("sub_count") or 0)]
        done = 0
        for floor in mains:
            if done >= max_pids:
                break
            pid = floor.get("pid")
            if not pid:
                continue
            existing = len([f for f in floors
                            if f.get("sub_id") and f.get("floor") == floor["floor"]])
            if existing >= (floor.get("sub_count") or 0):
                continue
            try:
                subs = self.floor_subs(tid, pid, floor["floor"], op_id or 0)
            except TiebaError as exc:
                if log:
                    log("楼中楼 #%s 失败：%s" % (floor["floor"], exc))
                continue
            have = {(f.get("floor"), f.get("sub_id")) for f in floors}
            for sub in subs:
                if (sub["floor"], sub["sub_id"]) not in have:
                    floors.append(sub)
            done += 1
            time.sleep(self.delay * 0.6)
        return done

    def search_threads(self, word: str, fname: str = "", pn: int = 1) -> dict:
        """全站/吧内搜索：/mo/q/search/thread，匿名可用，20 条/页。"""
        query = {"word": word, "pn": str(pn)}
        if fname:
            query["fname"] = fname
        url = BASE + "/mo/q/search/thread?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "x-requested-with": "XMLHttpRequest",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        payload = self._json(req)
        data = payload.get("data") or {}
        items = []
        for post in data.get("post_list") or []:
            user = post.get("user") or {}
            media = []
            for item in post.get("media") or []:
                if isinstance(item, dict) and item.get("big_pic"):
                    media.append({"url": item["big_pic"], "pic_id": str(item.get("id") or "")})
            items.append({
                "tid": _int(post.get("tid")),
                "pid": _int(post.get("pid")),
                "title": (post.get("title") or "").strip(),
                "content": _content_text(post.get("content")),
                "author": (user.get("show_nickname") or user.get("user_name") or "").strip(),
                "author_id": _int(user.get("user_id")),
                "create_ts": _int(post.get("create_time")),
                "last_ts": _int(post.get("time")),
                "reply_num": _int(post.get("post_num")),
                "forum_id": _int(post.get("forum_id")),
                "media": media,
            })
        return {"threads": items, "has_more": bool(data.get("has_more")),
                "page": _int(data.get("current_page")) or pn}

    # ------------------------------------------------------------- 吧 / 热榜
    def forum_threads(self, kw: str, pn: int = 1, rn: int = 30) -> dict:
        tbs = self.sync_tbs()
        params = {
            "kw": kw, "pn": str(pn), "rn": str(rn),
            "tbs": tbs, "subapp_type": "pc", "_client_type": "20",
        }
        params["sign"] = sign_params(params)
        headers = self.headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        req = urllib.request.Request(
            BASE + "/c/f/frs/page",
            data=urllib.parse.urlencode(params).encode("utf-8"),
            headers=headers,
        )
        payload = self._json(req)
        code = _int(payload.get("error_code"))
        if code:
            raise TiebaError("吧列表报错 %s：%s" % (code, payload.get("error_msg") or "未知"))
        items = []
        for thread in payload.get("thread_list") or []:
            author = thread.get("author") or {}
            items.append({
                "tid": _int(thread.get("id")),
                "title": (thread.get("title") or "").strip(),
                "reply_num": _int(thread.get("reply_num")),
                "author": (author.get("name_show") or author.get("name")
                           or thread.get("author_name") or "").strip(),
                "create_ts": _int(thread.get("create_time")),
                "last_ts": _int(thread.get("last_time") or thread.get("time")),
            })
        forum = payload.get("forum") or {}
        return {"forum": forum.get("name") or kw, "threads": items}

    def hot_topics(self, limit: int = 20) -> List[dict]:
        req = urllib.request.Request(
            BASE + "/hottopic/browse/topicList?res_type=1",
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        raw = self._open(req)
        text = raw.decode("utf-8", "replace")
        if any(m in text for m in BLOCK_MARKERS):
            raise TiebaError("热榜被安全验证拦了")
        pattern = re.compile(
            r'topic_id=(\d+)[^"]*"[^>]*class="topic-text"[^>]*>(.*?)</a>'
            r'(?:<span class="topic-num">(.*?)</span>)?', re.S)
        topics = []
        for match in pattern.finditer(text):
            tid, title, num = match.group(1), _clean(match.group(2)), match.group(3) or ""
            if not title:
                continue
            desc_match = re.search(
                r'<p class="topic-top-item-desc">(.*?)</p>', text[match.end():match.end() + 600], re.S)
            topics.append({
                "tid": tid,
                "title": unescape(title),
                "hot": _clean(num),
                "desc": _clean(desc_match.group(1)) if desc_match else "",
            })
            if len(topics) >= limit:
                break
        return topics
