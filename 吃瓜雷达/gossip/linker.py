#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多贴合并：把「主贴 / 回应 / 反转」串成同一个事件。

打分 = 参与者重合 + 时间邻近 + 共同词 + 互链 + 文本相似；
同吧且共享特征词额外加权，阈值默认 0.55。
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import util

STOP = set("""的 了 是 我 你 他 她 它 我们 你们 他们 在 就 都 也 和 与 这 那 有 没 不
一个 什么 怎么 为什么 因为 所以 但是 如果 已经 还是 就是 不是 可以 自己 现在 真的
楼主 回复 图片 帖子 贴吧 吧友 大家 感觉 觉得 知道 时候 一样 这样 那样 东西 事情
""".split())

REF_RE = re.compile(r"(?:tieba\.baidu\.com/p/|tid=|/p/)(\d{5,})")


def _tokens(text: str) -> Set[str]:
    text = (text or "").lower()
    raw = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fa5]{2,4}", text)
    return {t for t in raw if t not in STOP and len(t) >= 2}


def thread_features(db, tid: int) -> Optional[dict]:
    thread = db.get_thread(tid)
    if not thread:
        return None
    floors = db.list_floor_rows(tid, include_subs=False)
    if not floors:
        return None
    participants = set()
    op_text = []
    text_all = []
    for floor in floors:
        if floor.get("user_id"):
            participants.add("u%d" % floor["user_id"])
        elif floor.get("user_name"):
            participants.add("n" + floor["user_name"])
        content = floor.get("content") or ""
        text_all.append(content)
        if floor.get("floor") == 1 or floor.get("is_op"):
            op_text.append(content)
    title = thread.get("title") or ""
    tokens = _tokens(title) | _tokens("\n".join(op_text[:3]))
    refs = set(REF_RE.findall("\n".join(text_all[:80])))
    times = [f.get("ts") or 0 for f in floors if f.get("ts")]
    first_ts = min(times) if times else (thread.get("create_ts") or 0)
    return {
        "tid": tid,
        "title": title,
        "forum": thread.get("forum") or "",
        "author": thread.get("author") or "",
        "author_id": thread.get("author_id") or 0,
        "participants": participants,
        "tokens": tokens,
        "refs": refs,
        "create_ts": first_ts,
        "last_ts": max(times) if times else 0,
        "floors": len(floors),
        "reply_num": thread.get("reply_num") or 0,
    }


def pair_score(a: dict, b: dict) -> Tuple[float, dict]:
    pa, pb = a["participants"], b["participants"]
    inter = len(pa & pb)
    denom = max(1, min(len(pa), len(pb)))
    p_score = min(1.0, inter / denom * 2.0)
    delta = abs((a["create_ts"] or 0) - (b["create_ts"] or 0))
    t_score = math.exp(-delta / (72 * 3600.0)) if delta else 1.0
    shared = a["tokens"] & b["tokens"]
    e_score = min(1.0, len(shared) / 3.0)
    link = 0.0
    if b["tid"] in a["refs"] or a["tid"] in b["refs"]:
        link = 1.0
    ta, tb = a["tokens"], b["tokens"]
    union = ta | tb
    text_score = len(ta & tb) / len(union) if union else 0.0
    score = 0.30 * p_score + 0.25 * t_score + 0.20 * e_score + 0.15 * link + 0.10 * text_score
    same_forum = bool(a["forum"]) and a["forum"] == b["forum"]
    if same_forum:
        score += 0.05
    if same_forum and shared:
        score += 0.18
    return min(1.0, score), {
        "participants": round(p_score, 3), "time": round(t_score, 3),
        "entities": round(e_score, 3), "link": link,
        "text": round(text_score, 3), "shared": sorted(shared)[:8],
        "same_forum": same_forum,
    }


class _UF:
    def __init__(self, items: Iterable[int]) -> None:
        self.parent = {x: x for x in items}

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_stories(db, tids: Optional[List[int]] = None, threshold: float = 0.55,
                  min_threads: int = 2) -> List[dict]:
    if not tids:
        tids = [int(t["tid"]) for t in db.list_threads()]
    feats = [f for f in (thread_features(db, tid) for tid in tids) if f]
    if len(feats) < min_threads:
        return []
    uf = _UF([f["tid"] for f in feats])
    edges = []
    for i in range(len(feats)):
        for j in range(i + 1, len(feats)):
            score, detail = pair_score(feats[i], feats[j])
            edges.append((score, feats[i]["tid"], feats[j]["tid"], detail))
            if score >= threshold:
                uf.union(feats[i]["tid"], feats[j]["tid"])
    groups: Dict[int, List[dict]] = {}
    for feat in feats:
        groups.setdefault(uf.find(feat["tid"]), []).append(feat)

    db.delete_auto_stories()
    stories = []
    for group in groups.values():
        if len(group) < min_threads:
            continue
        group.sort(key=lambda f: f["create_ts"] or 0)
        ids = sorted(int(f["tid"]) for f in group)
        story_id = hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:12]
        forum = next((f["forum"] for f in group if f["forum"]), "")
        title = group[0]["title"] or "多贴事件"
        story = {
            "id": story_id,
            "title": title,
            "forum": forum,
            "status": "auto",
            "meta": {"tids": ids, "threads": len(group),
                     "score": round(max([e[0] for e in edges
                                        if e[1] in ids and e[2] in ids] or [0]), 3)},
        }
        threads = []
        op0 = group[0].get("author_id") or 0
        base_users = group[0]["participants"]
        for index, feat in enumerate(group):
            if index == 0:
                role = "主贴"
            elif op0 and feat.get("author_id") == op0:
                role = "楼主再回应"
            elif ("u%d" % feat.get("author_id")) in base_users:
                role = "当事人回应"
            else:
                role = "相关帖"
            threads.append({
                "tid": feat["tid"],
                "role": role,
                "score": 1.0 if index == 0 else 0.0,
            })
        db.save_story(story, threads)
        story["threads"] = db.story_threads(story_id)
        stories.append(story)
    return stories


def story_timeline(db, story_id: str, limit: int = 80) -> List[dict]:
    story = db.get_story(story_id)
    if not story:
        return []
    labels = {t["tid"]: "T%d" % (i + 1) for i, t in enumerate(story["threads"])}
    rows = []
    for thread in story["threads"]:
        tid = int(thread["tid"])
        label = labels[tid]
        floors = db.list_floor_rows(tid, include_subs=True)
        mains = [f for f in floors if not f.get("sub_id")]
        if not mains:
            continue
        first_ts = min([f.get("ts") for f in mains if f.get("ts")] or [
            thread.get("create_ts") or 0])
        rows.append({
            "ts": first_ts,
            "label": label, "tid": tid, "floor": 0, "user": thread.get("author") or "",
            "text": "开贴：《%s》" % (thread.get("title") or ""),
            "kind": "thread", "is_op": True, "media": 0,
        })
        ranked = sorted(mains, key=lambda f: (-(f.get("likes") or 0),
                                              -(f.get("sub_count") or 0),
                                              f.get("floor") or 0))
        picks = mains[:2] + ranked[:6]
        seen = set()
        for floor in picks:
            key = floor.get("floor")
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "ts": floor.get("ts") or 0,
                "label": label, "tid": tid, "floor": key,
                "user": floor.get("user_name") or "",
                "text": util.clip(floor.get("content") or "", 120),
                "kind": "post", "is_op": bool(floor.get("is_op")),
                "media": len(util.json_loads(floor.get("media_json"), []) or []),
            })
    rows.sort(key=lambda r: r["ts"])
    return rows[:limit]


def story_stats(db, story_id: str) -> dict:
    story = db.get_story(story_id)
    if not story:
        return {}
    users = set()
    floors = subs = 0
    first = last = 0
    for thread in story["threads"]:
        tid = int(thread["tid"])
        for floor in db.list_floor_rows(tid, include_subs=True):
            floors += 1 if not floor.get("sub_id") else 0
            subs += 1 if floor.get("sub_id") else 0
            if floor.get("user_id"):
                users.add(floor["user_id"])
            elif floor.get("user_name"):
                users.add(floor["user_name"])
            ts = floor.get("ts") or 0
            if ts:
                first = min(first or ts, ts)
                last = max(last, ts)
    return {"threads": len(story["threads"]), "floors": floors, "subs": subs,
            "participants": len(users), "first_ts": first, "last_ts": last,
            "span": (last - first) if first and last else 0}
