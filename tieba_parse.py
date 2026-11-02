#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""贴吧回复采集与解析。

三种入口，统一输出 reply dict：
  1. 已保存的帖子网页 (thread page HTML, data-field JSON)
  2. 纯文本 / CSV（手动粘贴、Excel 整理）
  3. cookie 直连抓取（可能被百度安全验证拦截，仅作备用）
"""
from __future__ import annotations

import csv
import hashlib
import html as htmllib
import json
import re
import time
import urllib.request
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FLOOR_TAIL_RE = re.compile(r'class="[^"]*tail-info[^"]*"[^>]*>(?P<body>[^<]{0,80})<')
FLOOR_NUM_RE = re.compile(r"(\d{1,6})\s*楼")

BLOCK_MARKERS = ("百度安全验证", "wappass.baidu.com", "请开启JavaScript并刷新此页面")


class ParseError(RuntimeError):
    """解析/抓取失败，带可操作提示。"""


# ---------------------------------------------------------------- 基础工具

def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def read_text(path: str) -> str:
    with open(path, "rb") as fh:
        return decode_bytes(fh.read())


def _int(value) -> int:
    if not value:
        return 0
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _clean_content(raw: str) -> str:
    if not raw:
        return ""
    text = str(raw)
    for tag in ("<br/>", "<br />", "<br>", "<BR/>", "<BR>"):
        text = text.replace(tag, "\n")
    text = re.sub(r"<[^>]{1,200}>", "", text)
    text = htmllib.unescape(text)
    return text.strip()


def _is_block_page(text: str) -> bool:
    return ("l_post" not in text) and any(m in text for m in BLOCK_MARKERS)


class _ThreadHTML(HTMLParser):
    """抓取所有带 data-field 的元素，记下它在原文里的绝对偏移。"""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self._line_starts = [0]
        for m in re.finditer(r"\n", text):
            self._line_starts.append(m.end())
        self.hits: List[Tuple[int, dict, str]] = []

    def _abs_offset(self) -> int:
        line, col = self.getpos()
        idx = line - 1
        if 0 <= idx < len(self._line_starts):
            return self._line_starts[idx] + col
        return 0

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        raw = data.get("data-field")
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return
        if "author" not in obj and "id" not in obj:
            return
        self.hits.append((self._abs_offset(), obj, data.get("class") or ""))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def _tail_floors(text: str) -> List[Tuple[int, int]]:
    """从 tail-info 片段里取「N楼」，返回 (绝对偏移, 楼层号)。"""
    out: List[Tuple[int, int]] = []
    for m in FLOOR_TAIL_RE.finditer(text):
        num = FLOOR_NUM_RE.search(m.group("body"))
        if num:
            out.append((m.start(), _int(num.group(1))))
    return out


def _record_from(obj: dict, is_op: bool, floor: Optional[int], tid: Optional[int], src: str) -> Optional[dict]:
    author = obj.get("author") or {}
    content = obj.get("content") or {}
    user_id = _int(author.get("user_id") or obj.get("id") or 0)
    user_name = str(author.get("user_name") or obj.get("name") or "").strip()
    nickname = str(author.get("user_nickname") or "").strip()
    portrait = str(author.get("portrait") or "").strip()
    if not user_id and not user_name and not nickname:
        return None
    return {
        "floor": floor,
        "user_id": user_id,
        "user_name": user_name,
        "nickname": nickname or user_name,
        "portrait": portrait,
        "date": str(content.get("date") or "").strip(),
        "content": _clean_content(content.get("content") or ""),
        "tid": tid or 0,
        "post_id": _int(content.get("post_id") or 0),
        "src": src,
        "_is_op": bool(is_op),
    }


def parse_html_text(text: str, src: str = "html", tid: Optional[int] = None) -> List[dict]:
    if not text:
        raise ParseError("%s：文件是空的" % src)
    if _is_block_page(text):
        raise ParseError(
            "%s：抓到的是百度安全验证页（未登录 / 被风控）。"
            "请在浏览器里登录后打开帖子，用 ⌘S 保存「网页，仅 HTML」，再用 --html 导入。" % src
        )
    if "l_post" not in text and "d_name" not in text:
        raise ParseError("%s：没有找到楼层数据（l_post / d_name），确认保存的是帖子详情页而非列表页" % src)

    parser = _ThreadHTML(text)
    try:
        parser.feed(text)
    except (ValueError, TypeError, AttributeError):
        pass

    hits = sorted(parser.hits, key=lambda h: h[0])
    primary = [h for h in hits if "author" in h[1]]
    if not primary:
        primary = hits

    # 同一 data-field 被内外层重复读到，按偏移去重
    seen_offsets = set()
    deduped = []
    for off, obj, cls in primary:
        if off in seen_offsets:
            continue
        seen_offsets.add(off)
        deduped.append((off, obj, cls))

    floors = _tail_floors(text)
    records: List[dict] = []
    for i, (off, obj, cls) in enumerate(deduped):
        end = deduped[i + 1][0] if i + 1 < len(deduped) else len(text)
        floor = None
        for foff, fnum in floors:
            if off <= foff < end:
                floor = fnum
                break
        is_op = "lz" in cls.split() or floor == 1
        rec = _record_from(obj, is_op, floor, tid, src)
        if rec:
            records.append(rec)

    if not records:
        raise ParseError("%s：解析出 0 条回复，页面结构可能变了" % src)

    # 没有任何楼层号时按顺序补
    if all(r["floor"] is None for r in records):
        for idx, rec in enumerate(records, start=1):
            rec["floor"] = idx
            if idx == 1:
                rec["_is_op"] = True
    else:
        last = 0
        for rec in records:
            if rec["floor"] is None:
                rec["floor"] = last + 1
            last = rec["floor"]

    return dedupe_records(records)


def parse_html_file(path: str, tid: Optional[int] = None) -> List[dict]:
    return parse_html_text(read_text(path), src="html:%s" % _basename(path), tid=tid)


def _basename(path: str) -> str:
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------- 文本 / CSV

_LINE_FLOOR_RE = re.compile(r"^\s*(\d{1,6})\s*楼\s*[:：、.,，|\-]?\s*(?P<rest>.+)$")
_LINE_FLOOR_BARE_RE = re.compile(r"^\s*(\d{1,6})\s+(?P<rest>\S.*)$")
_LINE_USER_RE = re.compile(r"^\s*@?(?P<u>[^\s@:：]{1,40})\s*[:：]\s*(?P<c>.*)$")
_LINE_SPLIT_RE = re.compile(r"^\s*@?(?P<u>[^\s@]{1,40})\s+(?P<c>.+)$")


def _split_user_content(rest: str) -> Tuple[Optional[str], str]:
    rest = rest.strip()
    if not rest:
        return None, ""
    m = _LINE_USER_RE.match(rest)
    if m and m.group("c").strip():
        return m.group("u").strip(), m.group("c").strip()
    m = _LINE_SPLIT_RE.match(rest)
    if m:
        return m.group("u").strip(), m.group("c").strip()
    return None, rest


def parse_text_text(text: str, src: str = "text", tid: Optional[int] = None) -> List[dict]:
    records: List[dict] = []
    floor_hint = 0
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        floor = None
        rest = line
        m = _LINE_FLOOR_RE.match(line)
        if not m:
            m = _LINE_FLOOR_BARE_RE.match(line)
        if m:
            floor = _int(m.group(1))
            rest = m.group("rest")
            floor_hint = max(floor_hint, floor)
        user, content = _split_user_content(rest)
        if not user and not content:
            continue
        if floor is None:
            floor_hint += 1
            floor = floor_hint
        records.append(
            {
                "floor": floor,
                "user_id": 0,
                "user_name": user or "",
                "nickname": user or "",
                "portrait": "",
                "date": "",
                "content": content,
                "tid": tid or 0,
                "post_id": 0,
                "src": "text:%d" % lineno,
                "_is_op": floor == 1,
            }
        )
    if not records:
        raise ParseError("%s：一行有效回复都没解析出来" % src)
    return dedupe_records(records, fallback_index=True)


_HEADER_MAP = (
    ("user_id", ("user_id", "uid", "用户id", "用户编号")),
    ("user_name", ("user_name", "username", "用户名", "用户", "账号", "id")),
    ("nickname", ("nickname", "昵称", "name", "名字")),
    ("floor", ("floor", "楼层", "楼", "层", "楼号")),
    ("content", ("content", "内容", "回复", "正文", "评论")),
    ("date", ("date", "时间", "日期")),
)


def _map_headers(fieldnames: Sequence[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    lowered = {str(f).strip().lower(): f for f in fieldnames}
    for target, aliases in _HEADER_MAP:
        if target in mapping:
            continue
        for alias in aliases:
            key = alias.lower()
            hit = lowered.get(key)
            if hit is None:
                for low, orig in lowered.items():
                    if key in low:
                        hit = orig
                        break
            if hit is not None:
                mapping[target] = hit
                break
    return mapping


def parse_csv_file(path: str, tid: Optional[int] = None) -> List[dict]:
    text = read_text(path)
    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        raise ParseError("%s：CSV 是空的" % path)
    mapping = _map_headers(rows[0].keys())
    if "user_name" not in mapping and "nickname" not in mapping:
        raise ParseError("%s：CSV 里找不到「用户名/昵称」列，表头是 %s" % (path, list(rows[0].keys())))

    def pick(row: dict, name: str) -> str:
        col = mapping.get(name)
        return str(row.get(col) or "").strip() if col else ""

    records: List[dict] = []
    for idx, row in enumerate(rows, start=1):
        user = pick(row, "user_name") or pick(row, "nickname")
        if not user:
            continue
        floor = _int(pick(row, "floor")) or idx
        records.append({
            "floor": floor,
            "user_id": _int(pick(row, "user_id")),
            "user_name": pick(row, "user_name"),
            "nickname": pick(row, "nickname") or user,
            "portrait": "",
            "date": pick(row, "date"),
            "content": pick(row, "content"),
            "tid": tid or 0,
            "post_id": 0,
            "src": "csv:%d" % (idx + 1),
            "_is_op": floor == 1,
        })
    if not records:
        raise ParseError("%s：CSV 里没有有效行" % path)
    return dedupe_records(records, fallback_index=True)


def dedupe_records(records: Iterable[dict], fallback_index: bool = False) -> List[dict]:
    """按 post_id 去重，缺失时按楼层+用户组合去重。"""
    out: List[dict] = []
    seen = set()
    for rec in records:
        pid = rec.get("post_id") or 0
        sig = (("pid", pid) if pid else
               ("f", rec.get("floor"), (rec.get("user_name") or "").lower(), rec.get("user_id") or 0))
        if sig not in seen:
            seen.add(sig)
            out.append(rec)
    out.sort(key=lambda r: (r.get("floor") or 0, r.get("post_id") or 0))
    return out


# 逆向自 pc-common/base：sign = md5(params 按 key 排序拼接 + 盐)
PC_SALT = "36770b1f34c9bbf2e7d1a99d2b82fa9e"
CLIENT_SALT = "tiebaclient!!!"


def pc_sign(params: Dict[str, str], salt: str = PC_SALT) -> str:
    raw = "".join("%s=%s" % (k, params[k]) for k in sorted(params)) + salt
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _pc_headers(tid: int, cookie: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "x-requested-with": "XMLHttpRequest",
        "Referer": "https://tieba.baidu.com/p/%d?fr=frs" % tid,
    }
    if cookie:
        headers["Cookie"] = cookie.strip()
    return headers


def pc_sync_tbs(tid: int, cookie: str = "", timeout: float = 15.0) -> str:
    """取 tbs（每次请求前都必须刷新，过期会 110001）。"""
    params = {"subapp_type": "pc", "_client_type": "20"}
    params["sign"] = pc_sign(params)
    url = "https://tieba.baidu.com/c/s/pc/sync?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_pc_headers(tid, cookie))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    tbs = ((data.get("data") or {}).get("anti") or {}).get("tbs")
    if not tbs:
        raise ParseError("拿不到 tbs：%s" % json.dumps(data, ensure_ascii=False)[:200])
    return tbs


def pc_page(tid: int, pn: int, tbs: str, cookie: str = "", r: int = 1,
            timeout: float = 20.0) -> dict:
    """POST /c/f/pb/page_pc —— 网页端帖子接口。r=1 为倒序（新→旧），r=2 为正序。"""
    params = {
        "pn": str(pn), "lz": "0", "r": str(r), "mark_type": "0", "back": "0",
        "fr": "frs", "kz": str(tid), "session_request_times": "1",
        "tbs": tbs, "subapp_type": "pc", "_client_type": "20",
    }
    params["sign"] = pc_sign(params)
    body = urllib.parse.urlencode(params).encode("utf-8")
    headers = _pc_headers(tid, cookie)
    headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
    req = urllib.request.Request("https://tieba.baidu.com/c/f/pb/page_pc", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _content_text(content) -> str:
    """接口的 content 是分段数组：0 文本 / 2 表情 / 3 图片 / 4 @人 / 10 语音。"""
    if isinstance(content, str):
        return _clean_content(content)
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
    return _clean_content("".join(parts))


def _fmt_time(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def fetch_pc_api(
    tid: int,
    cookie: str = "",
    pages: Optional[str] = None,
    delay: float = 0.5,
    log=None,
) -> List[dict]:
    """走网页端 /c/f/pb/page_pc 翻楼层。

    实测 r=1（倒序）匿名就能翻到底；r=2（正序）匿名只给第 1 页，
    页面上那句「登录后查看全部评论内容」就是 r=2 的分页门槛。
    带 BDUSS（--cookie-file）更稳，也能补回被截断的页。
    """
    tbs = pc_sync_tbs(tid, cookie)
    first = pc_page(tid, 1, tbs, cookie)
    if first.get("error_code"):
        raise ParseError("接口报错 %s：%s" % (first.get("error_code"), first.get("error_msg")))

    total_pages = _int((first.get("page") or {}).get("total_page")) or 1
    wanted = parse_pages_spec(pages, total_if_unknown=total_pages)
    op_id = _int(((first.get("thread") or {}).get("author") or {}).get("id"))
    users: Dict[int, dict] = {}
    records: List[dict] = []

    def absorb(payload: dict, label: str) -> int:
        for u in payload.get("user_list") or []:
            uid = _int(u.get("id"))
            if uid:
                users[uid] = {
                    "name": (u.get("name") or "").strip(),
                    "name_show": (u.get("name_show") or "").strip(),
                    "portrait": (u.get("portrait") or "").split("?")[0],
                }
        n = 0
        for post in payload.get("post_list") or []:
            uid = _int(post.get("author_id"))
            u = users.get(uid) or {}
            records.append({
                "floor": _int(post.get("floor")) or None,
                "user_id": uid,
                "user_name": u.get("name") or "",
                "nickname": u.get("name_show") or u.get("name") or "",
                "portrait": u.get("portrait") or "",
                "date": _fmt_time(post.get("time")),
                "content": _content_text(post.get("content")),
                "tid": tid,
                "post_id": _int(post.get("id")),
                "src": label,
                "_is_op": bool(op_id and uid == op_id),
            })
            n += 1
        return n

    if 1 in wanted:  # 第 1 页无论如何都要请求（拿 total_page / 楼主 ID），但只有用户要了才计入
        absorb(first, "pc:pn1")
    if log:
        log("第 1/%d 页 → %d 条" % (total_pages, len(records)))

    failed: List[int] = []
    for pn in wanted:
        if pn == 1 or pn > total_pages:
            continue
        try:
            payload = pc_page(tid, pn, tbs, cookie)
            if payload.get("error_code") == 110001:  # tbs 过期，换一个重试
                tbs = pc_sync_tbs(tid, cookie)
                payload = pc_page(tid, pn, tbs, cookie)
        except (urllib.error.URLError, ValueError, KeyError) as exc:
            failed.append(pn)
            if log:
                log("第 %d 页失败：%s" % (pn, exc))
            continue
        got = absorb(payload, "pc:pn%d" % pn)
        if log and (pn % 5 == 0 or got == 0):
            log("第 %d/%d 页 → 累计 %d 条" % (pn, total_pages, len(records)))
        if got == 0:  # total_page 会把末尾的空页也算进去
            break
        time.sleep(max(0.0, delay))

    if not records:
        raise ParseError("接口一条回复都没返回（可能需要 --cookie-file 带登录态）")
    if failed and log:
        log("警告：%d 页抓取失败（%s），结果可能不全"
            % (len(failed), ",".join(str(x) for x in failed[:10])))

    # 标记楼主：楼主本人发的所有楼层，含 1 楼正文
    for rec in records:
        if rec.get("floor") == 1:
            rec["_is_op"] = True
    return dedupe_records(records)


def parse_pages_spec(spec: Optional[str], total_if_unknown: int = 1) -> List[int]:
    if not spec:
        return list(range(1, total_if_unknown + 1))
    pages: List[int] = []
    for chunk in str(spec).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            pages.extend(range(lo, hi + 1))
        else:
            try:
                pages.append(int(chunk))
            except ValueError:
                continue
    return sorted(set(pages)) or [1]


def write_jsonl(records: Iterable[dict], path: str) -> int:
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def read_jsonl(path: str) -> List[dict]:
    out: List[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out
