#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切瓜引擎：本地规则分析 + 大模型报告。

本地模式是硬需求：没有 API Key 也要能出报告。它只做「楼层结构归纳」——
谁在高频发言、冲突句在哪、时间怎么分布、哪些说法没证据——不做事实认定。
有大模型时，把同一批材料交给模板，输出更像人写的日报。
"""
from __future__ import annotations

import re
import time
from typing import Dict, Iterable, List, Optional, Tuple

from collections import defaultdict

from . import linker, llm, prompts, util

# ---------------------------------------------------------------- 信号词典
SIGNALS: List[Tuple[str, re.Pattern, float]] = [
    ("反转", re.compile(r"反转|打脸|翻车|实锤|锤了|破防|急了|删帖|封号|锁帖|道歉|澄清|"
                        r"回应|证据|截图|律师函|报警|起诉|网暴|爆料|真相|承认"), 2.0),
    ("开撕", re.compile(r"离谱|逆天|下头|洗地|洗白|别装了|你自己|编的|假的|戏真多|矫情|"
                        r"自找|活该|德不配位|恶心|滚|闭嘴|素质"), 1.6),
    ("站队", re.compile(r"支持楼主|站楼主|楼主没错|楼主委屈|理解楼主|心疼|抱抱|加油|"
                        r"说得对|确实|有道理|不怪你|正常"), 1.0),
    ("中立", re.compile(r"中立|路人|别站队|都有问题|观望|不懂|等后续|吃瓜|插眼|"
                        r"让子弹飞|mark"), 0.6),
    ("乐子", re.compile(r"哈哈哈|笑死|绷不住|难绷|乐子|抽象|典|草|🤣|😂|🐶"), 1.2),
]

CONFLICT_RE = re.compile(r"凭什么|难道|为什么不|你自己|你说|不是.*吗|敢不敢|有本事|"
                         r"笑死|急了|破防|凭什么|谁给的|装|洗")
HEDGE_RE = re.compile(r"听说|据说|据传|网传|朋友说|有人说|好像|似乎|可能|据我了解|"
                      r"不保真|不确定|存疑")
FACT_RE = re.compile(r"\d{1,2}[月日号]|\d{1,2}[:：]\d{2}|\d+\s*(万|块|元|岁|年|楼|次|个)")
WATER_RE = re.compile(r"^(顶|dd|ddd|111|666|哈哈+|hhh+|插眼|mark|打卡|冒泡|路过|"
                      r"支持|赞|好|在|1|2|3|\+1|👀|🐴|🍉)\W*$", re.I)

MEME_TERMS = ["笑死", "绷不住", "离谱", "逆天", "典", "破防", "急了", "下头", "抽象",
              "乐子", "阴阳", "建议", "分手", "报警", "证据", "截图", "删帖", "尊重",
              "祝福", "同情", "佩服", "无语", "瓜", "蹲", "插眼"]

STOP_USERS = {"匿名", ""}


def is_water(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return True
    stripped = re.sub(r"\[图片\]|\[语音\]|\[表情\]", "", text).strip()
    if len(stripped) <= 2:
        return True
    return bool(WATER_RE.match(stripped))


def score_floor(floor: dict) -> float:
    text = floor.get("content") or ""
    if is_water(text):
        return -5.0
    score = min(len(text), 240) / 50.0
    for _name, pattern, weight in SIGNALS:
        if pattern.search(text):
            score += weight * 1.6
    score += 1.2 * min(int(floor.get("likes") or 0), 30)
    score += 0.9 * min(int(floor.get("sub_count") or 0), 30)
    if floor.get("is_op"):
        score += 0.6
    if text.rstrip().endswith(("？", "?")):
        score += 0.3
    if len(text) >= 80:
        score += 0.8
    return score


def camp_of(text: str) -> str:
    best, best_weight = "其他", 0.0
    for name, pattern, weight in SIGNALS:
        if pattern.search(text) and weight > best_weight:
            best, best_weight = name, weight
    return best


def compute_stats(floors: List[dict]) -> dict:
    mains = [f for f in floors if not f.get("sub_id")]
    users = {}
    first_ts = last_ts = 0
    op_floors = 0
    for floor in mains:
        ts = int(floor.get("ts") or 0)
        if ts:
            first_ts = min(first_ts or ts, ts)
            last_ts = max(last_ts, ts)
        key = str(floor.get("user_id") or floor.get("user_name") or "匿名")
        users[key] = users.get(key, 0) + 1
        if floor.get("is_op"):
            op_floors += 1
    buckets: Dict[str, int] = {}
    for floor in mains:
        ts = int(floor.get("ts") or 0)
        if ts:
            label = util.fmt_ts(ts, "%m-%d %H:00")
            buckets[label] = buckets.get(label, 0) + 1
    series = [{"label": k, "count": buckets[k]} for k in sorted(buckets)]
    return {
        "floors": len(mains),
        "subs": len(floors) - len(mains),
        "participants": len(users),
        "op_floors": op_floors,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "span": (last_ts - first_ts) if first_ts and last_ts else 0,
        "series": series,
        "top_users": sorted(
            ({"uid": k, "count": v} for k, v in users.items()),
            key=lambda x: -x["count"])[:6],
    }


def top_terms(floors: List[dict], limit: int = 8) -> List[Tuple[str, int]]:
    text = "\n".join(f.get("content") or "" for f in floors)
    found = []
    for term in MEME_TERMS:
        count = text.count(term)
        if count >= 2:
            found.append((term, count))
    found.sort(key=lambda kv: -kv[1])
    return found[:limit]


def select_floors(floors: List[dict], budget_chars: int,
                 max_floors: int = 120) -> List[dict]:
    """按「楼主观感 + 冲突密度 + 信息量」挑料，再按时序排回去。"""
    mains = [f for f in floors if not f.get("sub_id")]
    if not mains:
        return []
    first = mains[0]
    starred = [f for f in mains if f.get("starred")]
    ranked = sorted(mains, key=lambda f: (-score_floor(f), f.get("floor") or 0))

    picked: Dict[int, dict] = {}
    used = 0

    def take(floor: dict) -> bool:
        nonlocal used
        key = int(floor.get("floor") or 0)
        if key in picked:
            return True
        body = "".join([floor.get("content") or ""] +
                       [s.get("content") or "" for s in (floor.get("subs") or [])[:3]])
        cost = len(body) + 80
        if used + cost > budget_chars or len(picked) >= max_floors:
            return False
        picked[key] = floor
        used += cost
        return True

    take(first)
    for floor in starred:
        take(floor)
    for floor in ranked:
        if not take(floor) and len(picked) >= max_floors:
            break
    return sorted(picked.values(), key=lambda f: f.get("floor") or 0)


def _quote(floor: dict, limit: int = 120) -> str:
    text = re.sub(r"\n+", " ", (floor.get("content") or "").strip())
    text = re.sub(r"\[图片\]|\[语音\]", "", text).strip() or "（只发了图）"
    return util.clip(text, limit)


def _source(floor: dict) -> str:
    return "#%s @%s" % (floor.get("floor"), floor.get("user_name") or "匿名")


def _timeline_score(floor: dict) -> float:
    """时间轴代表楼层：偏好楼主 / 有赞 / 不太长的现场描述。"""
    score = score_floor(floor)
    if floor.get("is_op"):
        score += 2.5
    score += min(int(floor.get("likes") or 0), 10) * 0.25
    score -= max(0, len(floor.get("content") or "") - 160) / 30.0
    return score


def _timeline(floors: List[dict], stats: dict, buckets: int = 10) -> List[dict]:
    timed = [f for f in floors if (f.get("ts") or 0) and not f.get("sub_id")]
    if not timed:
        return []
    span = (stats.get("last_ts") or 0) - (stats.get("first_ts") or 0)
    width = max(600, int(span / max(3, buckets)) // 600 * 600) if span else 3600
    grouped: Dict[int, List[dict]] = {}
    for floor in timed:
        index = int(((floor["ts"] - (stats.get("first_ts") or floor["ts"])) // width))
        grouped.setdefault(index, []).append(floor)
    out = []
    for index in sorted(grouped):
        chunk = grouped[index]
        take = sorted(chunk, key=_timeline_score, reverse=True)
        chosen = []
        for floor in take:
            if floor.get("user_name") in [c.get("user_name") for c in chosen]:
                continue
            chosen.append(floor)
            if len(chosen) >= 2:
                break
        chosen.sort(key=lambda f: f.get("ts") or 0)
        for floor in chosen:
            out.append({
                "time": util.fmt_ts(floor.get("ts"), "%m-%d %H:%M"),
                "text": _quote(floor, 90),
                "source": _source(floor),
                "is_op": bool(floor.get("is_op")),
            })
    return out


def analyze(floors: List[dict]) -> dict:
    mains = [f for f in floors if not f.get("sub_id")]
    stats = compute_stats(floors)
    scored = [f for f in mains if not is_water(f.get("content"))]
    ranked = sorted(scored, key=lambda f: (-score_floor(f), f.get("floor") or 0))

    quotes: List[dict] = []
    seen_users = set()
    for floor in ranked:
        user = floor.get("user_name") or "匿名"
        if user in seen_users and len(quotes) < 3:
            continue
        quotes.append(floor)
        seen_users.add(user)
        if len(quotes) >= 6:
            break

    conflicts = [f for f in ranked
                 if CONFLICT_RE.search(f.get("content") or "")
                 and len(f.get("content") or "") <= 220][:5]
    used_floors = {f.get("floor") for f in conflicts}
    claims_hedged = [f for f in ranked
                     if HEDGE_RE.search(f.get("content") or "")
                     and len(f.get("content") or "") <= 220
                     and f.get("floor") not in used_floors][:4]
    used_floors |= {f.get("floor") for f in claims_hedged}
    claims_fact = [f for f in ranked
                   if FACT_RE.search(f.get("content") or "")
                   and not HEDGE_RE.search(f.get("content") or "")
                   and len(f.get("content") or "") <= 220
                   and f.get("floor") not in used_floors][:4]
    op_floors = [f for f in mains if f.get("is_op")]

    camps = {"反转/吃瓜": 0, "开撕": 0, "站队": 0, "中立": 0, "乐子": 0, "其他": 0}
    for floor in mains:
        camps[camp_of(floor.get("content") or "")] = \
            camps.get(camp_of(floor.get("content") or ""), 0) + 1
    classified = sum(v for k, v in camps.items() if k != "其他") or 1

    return {
        "stats": stats,
        "quotes": quotes,
        "conflicts": conflicts,
        "hedged": claims_hedged,
        "facts": claims_fact,
        "op_floors": op_floors,
        "camps": camps,
        "classified": classified,
        "timeline": _timeline(mains, stats),
        "terms": top_terms(mains),
        "peak": max(stats["series"], key=lambda x: x["count"]) if stats["series"] else None,
    }


# ---------------------------------------------------------------- 一句话生成
def one_liner(thread: dict, analysis: dict) -> str:
    stats = analysis["stats"]
    peak = analysis.get("peak")
    ops = analysis.get("op_floors") or []
    hook = _quote(ops[0], 34) if ops else (thread.get("title") or "这栋楼")
    last_op = int((ops[-1].get("ts") if ops else 0) or 0)
    if last_op and time.time() - last_op < 6 * 3600:
        status = "楼主还在楼里"
    elif last_op:
        status = "楼主已经 %s 没露面" % util.human_span(time.time() - last_op)
    else:
        status = "楼主没怎么露面"
    return ("「%s」——%d 楼里 %d 个 ID 围着这句话吵，%s 最挤，%s。" % (
        hook, stats["floors"], stats["participants"],
        ("「%s」" % peak["label"]) if peak else "开楼那会儿", status))


VERDICTS = [
    "围观可以，别把自己围进去。",
    "瓜是别人的，血压是自己的。",
    "凡是没截图的指控，都先按 50% 打折听。",
    "最响的那一楼，往往不是最有信息量的那一楼。",
    "时间线一拉直，很多「实锤」就自动变成「听说」。",
    "这楼里最稀缺的不是观点，是出处。",
]


def _verdict(thread: dict) -> str:
    return VERDICTS[util.stable_hash("v" + str(thread.get("tid")), len(VERDICTS))]


def _pct(part: int, total: int) -> str:
    if not total:
        return "—"
    return "%d%%" % round(part * 100.0 / total)


# ---------------------------------------------------------------- 本地报告
def _header(thread: dict, stats: dict, engine: str) -> str:
    return (
        "# 🍉 %s · %s\n\n"
        "> %s · %s · 楼主 @%s · %d 楼 / %d 人 · 跨度 %s\n"
        "> 引擎：%s · 生成于 %s\n\n"
        % (thread.get("mode_name") or "吃瓜报告", thread.get("title") or "无标题",
           thread.get("tid"), thread.get("forum") or "贴吧",
           thread.get("author") or "未知", stats["floors"], stats["participants"],
           util.human_span(stats["span"]), engine, util.fmt_ts(time.time(), "%Y-%m-%d %H:%M"))
    )


def _timeline_table(analysis: dict, limit: int = 12) -> str:
    rows = analysis["timeline"][:limit]
    if not rows:
        return "_材料里没有可用的时间戳。_\n"
    lines = ["| 时间 | 现场 | 出处 |", "| --- | --- | --- |"]
    for row in rows:
        text = row["text"].replace("|", "／")
        lines.append("| %s | %s%s | %s |" % (
            row["time"], "**楼主：**" if row["is_op"] else "", text, row["source"]))
    return "\n".join(lines) + "\n"


def _quotes_block(analysis: dict) -> str:
    blocks = []
    replies = [f for f in analysis["quotes"] if not f.get("is_op")]
    picks = (replies or analysis["quotes"])[:5]
    for floor in picks:
        meta = []
        if floor.get("likes"):
            meta.append("赞 %d" % floor["likes"])
        if floor.get("sub_count"):
            meta.append("楼中楼 %d" % floor["sub_count"])
        tail = "（%s）" % "，".join(meta) if meta else ""
        blocks.append("> **%s**%s\n> %s\n" % (_source(floor), tail, _quote(floor, 130)))
    return "\n".join(blocks) if blocks else "_暂时没有能被单独摘出来的神回复。_\n"


def _camp_table(analysis: dict) -> str:
    camps = analysis["camps"]
    total = max(1, analysis["stats"]["floors"])
    lines = ["| 阵营 | 楼层占比 | 备注 |", "| --- | --- | --- |"]
    notes = {
        "反转/吃瓜": "在等实锤、复盘时间线",
        "开撕": "直接对线，火力最猛",
        "站队": "明确支持楼主/当事人",
        "中立": "观望、让子弹飞",
        "乐子": "纯看戏，金句产地",
        "其他": "就事论事或纯水",
    }
    for name in ["反转/吃瓜", "开撕", "站队", "中立", "乐子", "其他"]:
        count = camps.get(name, 0)
        label = "未识别" if name == "其他" else name
        lines.append("| %s | %d 楼（%s） | %s |" % (
            label, count, _pct(count, total), notes[name]))
    lines.append("")
    lines.append("_按楼层文本里的情绪词粗分，一个楼层可能同时符合多类；「未识别」= 没命中任何阵营词，占多数很正常。_")
    return "\n".join(lines) + "\n"


def _evidence(analysis: dict) -> str:
    out = []
    if analysis["facts"]:
        out.append("**能对得上号的具体陈述**（带时间/数字/地点，可回楼核对）")
        for floor in analysis["facts"][:3]:
            out.append("- %s：%s" % (_source(floor), _quote(floor, 90)))
    if analysis["hedged"]:
        out.append("\n**单方说法 / 待核实**（有「听说/据说/可能」这类词）")
        for floor in analysis["hedged"][:3]:
            out.append("- %s：%s" % (_source(floor), _quote(floor, 90)))
    if analysis["conflicts"]:
        out.append("\n**冲突最密集的楼层**（情绪浓度高，不等于事实）")
        for floor in analysis["conflicts"][:3]:
            out.append("- %s：%s" % (_source(floor), _quote(floor, 90)))
    return "\n".join(out) + "\n" if out else "_材料里没有可判定的证据型楼层。_\n"


def _op_section(analysis: dict) -> str:
    ops = analysis["op_floors"]
    if not ops:
        return "_楼主没有在这批楼层里发言。_\n"
    first = ops[0]
    out = ["- **开楼**：%s" % _quote(first, 160)]
    if len(ops) > 1:
        out.append("- **后续补充**：%d 次发言，最后一条在 %s" % (
            len(ops), util.fmt_ts(ops[-1].get("ts"), "%m-%d %H:%M")))
        for floor in ops[1:4]:
            out.append("- %s：%s" % (_source(floor), _quote(floor, 100)))
    return "\n".join(out) + "\n"


def _disputes(analysis: dict) -> str:
    if not analysis["conflicts"]:
        return "_没有明显的对线楼层，大家主要在补充信息。_\n"
    out = []
    for index, floor in enumerate(analysis["conflicts"][:4], 1):
        out.append("%d. **%s** —— %s" % (
            index, util.clip(floor.get("content") or "", 46), _source(floor)))
    out.append("\n_（以上是从冲突密度最高的楼层里抽的，顺序按火力排，不代表本报告立场。）_")
    return "\n".join(out) + "\n"


def _terms_line(analysis: dict) -> str:
    terms = analysis["terms"]
    if not terms:
        return ""
    return "、".join("%s×%d" % (t, c) for t, c in terms)


def offline_report(mode: str, thread: dict, floors: List[dict]) -> str:
    analysis = analyze(floors)
    stats = analysis["stats"]
    engine = "本地规则切瓜（未调用大模型，只做结构归纳）"
    head = _header(thread, stats, engine)
    if mode == "quick":
        camps = analysis["camps"]
        total = max(1, analysis["classified"])
        replies = [f for f in analysis["quotes"] if not f.get("is_op")] or analysis["quotes"]
        return head + "\n".join([
            "**1. 一句话概括**：%s" % one_liner(thread, analysis),
            "**2. 时间线（简版）**",
            *["- %s · %s（%s）" % (r["time"], r["text"], r["source"])
              for r in analysis["timeline"][:4]],
            "**3. 谁对谁、吵什么**：%d 个ID、%d 楼，冲突最集中在 %s。" % (
                stats["participants"], stats["floors"],
                "、".join(_source(f) for f in analysis["conflicts"][:2]) or "没有明显对线"),
            "**4. 站队情况**：明确表态的 %d 楼里，开撕 %s ／ 站队 %s ／ 中立 %s ／ 乐子 %s。" % (
                analysis["classified"], _pct(camps["开撕"], total), _pct(camps["站队"], total),
                _pct(camps["中立"], total), _pct(camps["乐子"], total)),
            "**5. 反转或神回复**：%s" % (_quote(replies[0], 120)
                                      if replies else "暂未出现硬反转"),
            "",
            "> %s" % _verdict(thread),
        ]) + "\n"

    parts = [head]
    if mode == "structured":
        parts.append("**【一句话总结】**\n%s\n" % one_liner(thread, analysis))
        parts.append("**【时间线】**\n%s" % _timeline_table(analysis))
        parts.append("**【关键人物与立场】**\n%s" % _op_section(analysis))
        parts.append("**【核心争议点】**\n%s" % _disputes(analysis))
        parts.append("**【网友阵营分布】**\n%s" % _camp_table(analysis))
        parts.append("**【证据强弱与待核实】**\n%s" % _evidence(analysis))
        parts.append("**【一句话锐评】**\n> %s\n" % _verdict(thread))
        return "\n".join(parts)

    if mode == "review":
        parts.append("## 事件时间线全景图\n")
        parts.append("时间范围：%s → %s（跨度 %s）\n" % (
            util.fmt_ts(stats["first_ts"], "%Y-%m-%d %H:%M") or "未知",
            util.fmt_ts(stats["last_ts"], "%Y-%m-%d %H:%M") or "未知",
            util.human_span(stats["span"])))
        peak = analysis["peak"]
        parts.append("舆论高峰：%s（%d 楼）\n" % (
            peak["label"] if peak else "无明显峰值", peak["count"] if peak else 0))
        parts.append(_timeline_table(analysis, limit=16))
        parts.append("## 多维信息汇总\n")
        parts.append("### 1. 当事人表态\n%s" % _op_section(analysis))
        parts.append("### 2. 各方观点对照\n%s" % _camp_table(analysis))
        parts.append("### 3. 情绪词分布\n%s\n" % (_terms_line(analysis) or "材料太少，统计不出来"))
        parts.append("## 争议焦点与事实核查\n%s" % _evidence(analysis))
        parts.append("## 当前状态 + 待观察点\n")
        last_ts = stats["last_ts"]
        idle = int(time.time() - last_ts) if last_ts else 0
        parts.append("- 最后一楼停在 %s（%s前）。" % (
            util.fmt_ts(last_ts, "%m-%d %H:%M") or "未知", util.human_ago(last_ts) or "未知"))
        parts.append("- 待观察：楼主是否继续更新、是否有当事人回应、是否出现截图/第三方佐证。")
        parts.append("- 数据缺口：贴吧会吞楼，材料里缺失的楼层号可能永远补不回来。\n")
        parts.append("> %s\n" % _verdict(thread))
        return "\n".join(parts)

    # daily（默认）
    parts.append("## 一句话吃瓜\n%s\n" % one_liner(thread, analysis))
    parts.append("## 时间轴\n%s" % _timeline_table(analysis))
    parts.append("## 楼主到底在经历什么\n%s" % _op_section(analysis))
    parts.append("## 争议焦点\n%s" % _disputes(analysis))
    parts.append("## 神回复 / 反转位\n%s" % _quotes_block(analysis))
    parts.append("## 阵营分布\n%s" % _camp_table(analysis))
    if analysis["terms"]:
        parts.append("## 情绪浓度\n%s\n" % _terms_line(analysis))
    parts.append("## 证据强弱\n%s" % _evidence(analysis))
    parts.append("## 一句话锐评\n> %s\n" % _verdict(thread))
    return "\n".join(parts)


# ---------------------------------------------------------------- 入口
def generate(db, tid: int, mode: str = "daily", provider: str = "auto",
             neutral: bool = False, extra_note: str = "",
             progress=None) -> dict:
    started = time.time()
    thread = db.get_thread(tid)
    if not thread:
        raise ValueError("帖 %s 还没入库，先添加监控" % tid)
    floors = db.grouped_floors(tid)
    if not floors:
        raise ValueError("帖 %s 一条楼层都没有" % tid)
    meta = prompts.MODES.get(mode) or prompts.MODES["daily"]
    thread = dict(thread)
    thread["mode_name"] = meta["name"]
    stats = compute_stats(floors)

    cfg = llm.resolve(db)
    use_llm = provider not in ("local", "offline") and (
        (cfg.get("type") == "cli" and llm.cli_available(cfg.get("command")))
        or (cfg.get("type") not in (None, "", "none", "cli") and cfg.get("api_key"))
    )
    if provider not in ("local", "offline", "auto") and not use_llm:
        use_llm = False

    engine_note = "本地规则引擎"
    llm_meta = {}
    error = ""
    if use_llm:
        selected = select_floors(floors, meta["budget"])
        prompt = prompts.build_prompt(mode, thread, stats, selected, stats["floors"],
                                      neutral=neutral, extra_note=extra_note)
        if progress:
            progress("已选 %d 楼，%d 字材料，呼叫模型…" % (len(selected), prompt["chars"]))
        try:
            text, llm_meta = llm.chat(cfg, prompt["system"], prompt["user"])
            text = _strip_wrapper(text)
            markdown = "%s\n\n---\n\n_材料：%d 楼节选 ｜ 模型：%s ｜ 用时 %.1fs_" % (
                text, len(selected), llm_meta.get("model") or cfg.get("model"),
                time.time() - started)
            engine_note = "大模型：%s" % (llm_meta.get("model") or cfg.get("model"))
        except Exception as exc:  # 模型挂了也要出东西
            error = str(exc)
            markdown = offline_report(mode, thread, floors)
            markdown += "\n\n> ⚠️ 模型调用失败，已退回本地规则报告：%s\n" % util.clip(error, 160)
            engine_note = "本地兜底（模型失败）"
            if progress:
                progress("模型失败，已切本地：%s" % util.clip(error, 80))
    else:
        markdown = offline_report(mode, thread, floors)
        if provider == "auto":
            error = "未配置模型，使用本地规则引擎"

    elapsed = round(time.time() - started, 1)
    title = "%s · %s" % (meta["name"], thread.get("title") or "吃瓜")
    report = {
        "tid": tid,
        "mode": mode,
        "provider": "llm" if use_llm and not error else "local",
        "model": llm_meta.get("model") or (cfg.get("model") if use_llm else "规则引擎"),
        "title": title,
        "markdown": markdown,
        "meta": {
            "engine": engine_note,
            "mode_name": meta["name"],
            "floors": stats["floors"],
            "participants": stats["participants"],
            "span": stats["span"],
            "neutral": bool(neutral),
            "chars": len(markdown),
            "seconds": elapsed,
            "error": error,
            "thread_title": thread.get("title"),
            "forum": thread.get("forum"),
            "author": thread.get("author"),
        },
    }
    report["id"] = db.save_report(report)
    return report


def _strip_wrapper(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    text = re.sub(r"^(好的|没问题|以下是)[^\n]{0,40}\n+", "", text)
    return text.strip()


# ---------------------------------------------------------------- 多贴事件
def _story_pack(db, story: dict) -> dict:
    """把事件里的每贴整理成：标签、楼层、图片分析、统计。"""
    threads = story.get("threads") or []
    labels, floors_by_tid, media_by_tid, stats_by_tid = {}, {}, {}, {}
    for index, thread in enumerate(threads, 1):
        tid = int(thread["tid"])
        labels[tid] = "T%d" % index
        floors = db.grouped_floors(tid)
        stats_by_tid[tid] = compute_stats(floors)
        media_map = defaultdict(list)
        for row in db.media_rows(tid, limit=600):
            if row.get("analysis"):
                media_map[(int(row.get("floor") or 0), int(row.get("sub_id") or 0))].append(row)
        media_by_tid[tid] = dict(media_map)
        chosen = select_floors(floors, 3200, max_floors=60)
        have = {int(f.get("floor") or 0) for f in chosen}
        for floor in floors:
            if floor.get("media_json") not in (None, "", "[]"):
                if int(floor.get("floor") or 0) not in have:
                    chosen.append(floor)
        chosen.sort(key=lambda f: f.get("floor") or 0)
        floors_by_tid[tid] = chosen
    return {"labels": labels, "floors": floors_by_tid,
            "media": media_by_tid, "stats": stats_by_tid}


def _story_media_rows(db, story: dict, labels: dict) -> List[dict]:
    items = []
    counters = {}
    for thread in story.get("threads") or []:
        tid = int(thread["tid"])
        label = labels.get(tid, "T?")
        for row in db.media_rows(tid, limit=400):
            analysis = row.get("analysis") or {}
            if not analysis:
                continue
            text_probe = ((analysis.get("ocr_text") or "") + " "
                          + (analysis.get("description") or "")).lower()
            if "api error" in text_probe or "malformed" in text_probe:
                continue
            if len((analysis.get("ocr_text") or "").strip()) < 3 and not analysis.get("description"):
                continue
            key = (label, row.get("floor") or 0, row.get("sub_id") or 0)
            counters[key] = counters.get(key, 0) + 1
            items.append({
                "label": label,
                "floor": row.get("floor") or 0,
                "sub_id": row.get("sub_id") or 0,
                "index": counters[key],
                "type": analysis.get("type") or "other",
                "ocr": analysis.get("ocr_text") or "",
                "desc": analysis.get("description") or "",
                "confidence": analysis.get("confidence") or 0,
            })
    items.sort(key=lambda x: (-(x["confidence"]), x["label"], x["floor"]))
    return items


def _story_media_table(items: List[dict], limit: int = 8) -> str:
    if not items:
        return "_还没有识图结果：在帖子页点「识图」，或配置多模态 API 后重跑。_\n"
    lines = ["| 出处 | 类型 | 图中内容（OCR / 描述） |", "| --- | --- | --- |"]
    for item in items[:limit]:
        text = util.clip((item["ocr"] or "").replace("\n", " "), 110)
        desc = util.clip(item["desc"] or "", 70)
        content = "；".join(x for x in (text, desc) if x) or "（无文字）"
        content = content.replace("|", "／")
        lines.append("| %s#%s-图%d | %s | %s |" % (
            item["label"], item["floor"], item.get("index") or 1, item["type"], content))
    return "\n".join(lines) + "\n"


def _story_roles_table(story: dict) -> str:
    lines = ["| 标签 | 标题 | 楼主 | 楼层 | 角色 |", "| --- | --- | --- | --- | --- |"]
    for index, thread in enumerate(story.get("threads") or [], 1):
        lines.append("| T%d | %s | %s | %s | %s |" % (
            index, util.clip(thread.get("title") or "", 30) or "无标题",
            thread.get("author") or "未知", thread.get("floor_count") or 0,
            thread.get("role") or "相关帖"))
    return "\n".join(lines) + "\n"


def _story_timeline_table(timeline: List[dict], limit: int = 18) -> str:
    if not timeline:
        return "_没有可用的时间线。_\n"
    lines = ["| 时间 | 贴子 | 现场 | 出处 |", "| --- | --- | --- | --- |"]
    for row in timeline[:limit]:
        lines.append("| %s | %s | %s | %s#%s @%s |" % (
            util.fmt_ts(row.get("ts"), "%m-%d %H:%M"), row["label"],
            util.clip((row.get("text") or "").replace("|", "／").replace("\n", " "), 90),
            row["label"], row["floor"], row.get("user") or "匿名"))
    return "\n".join(lines) + "\n"


def _story_one_liner_text(story: dict, stats: dict) -> str:
    threads = story.get("threads") or []
    return "「%s」——围绕这件事有 %d 个帖子、%d 楼、%d 人参与，跨度 %s。" % (
        (threads[0].get("title") if threads else "多贴事件"),
        len(threads), stats.get("floors") or 0, stats.get("participants") or 0,
        util.human_span(stats.get("span") or 0))


def _story_conflicts_text(pack: dict) -> str:
    lines = []
    for thread in pack.get("thread_analysis") or []:
        label = thread["label"]
        for floor in thread["analysis"]["conflicts"][:2]:
            lines.append("- **%s#%s @%s**：%s" % (
                label, floor.get("floor"), floor.get("user_name") or "匿名",
                _quote(floor, 90)))
    return "\n".join(lines) + "\n" if lines else "_没有明显的对线楼层。_\n"


def _story_evidence_text(pack: dict) -> str:
    lines = []
    media_items = pack.get("media_items") or []
    if media_items:
        lines.append("**图片证据**：%d 张已识别（详见上表）；OCR 与文字对不上的地方需要人工复核。"
                     % len(media_items))
    for thread in pack.get("thread_analysis") or []:
        label = thread["label"]
        for floor in thread["analysis"]["hedged"][:2]:
            lines.append("- %s#%s 单方说法：%s" % (
                label, floor.get("floor"), _quote(floor, 90)))
    return "\n".join(lines) + "\n" if lines else "_材料不足以判定证据强弱。_\n"


def offline_story_report(mode: str, story: dict, pack: dict, stats: dict,
                         timeline: List[dict]) -> str:
    labels = pack["labels"]
    engine = "本地规则引擎（多贴合并，未调用大模型）"
    head = ("# 🍉 %s · 事件：%s\n\n"
            "> 合并 %d 贴 ｜ %d 楼 / %d 人 ｜ 跨度 %s ｜ %s\n"
            "> 引擎：%s · 生成于 %s\n\n" % (
                "吃瓜日报" if mode in ("daily", "structured") else "事件复盘",
                story.get("title") or "多贴事件", len(story.get("threads") or []),
                stats.get("floors") or 0, stats.get("participants") or 0,
                util.human_span(stats.get("span") or 0), story.get("forum") or "贴吧",
                engine, util.fmt_ts(time.time(), "%Y-%m-%d %H:%M")))

    if mode == "quick":
        body = [
            "**1. 一句话概括**：%s" % _story_one_liner_text(story, stats),
            "**2. 时间线（简版）**",
        ]
        for row in timeline[:5]:
            body.append("- %s · %s（%s#%s）" % (
                util.fmt_ts(row.get("ts"), "%m-%d %H:%M"),
                util.clip(row.get("text") or "", 70), row["label"], row["floor"]))
        body.extend([
            "**3. 谁对谁、吵什么**：%d 贴、%d 楼，详见多贴时间线。" % (
                len(story.get("threads") or []), stats.get("floors") or 0),
            "**4. 站队情况**：本地规则不做比例推断，按各贴正文人工判断。",
            "**5. 反转或神回复**：见「图片证据」与「争议焦点」。",
            "",
            "> %s" % VERDICTS[util.stable_hash(story.get("id") or "", len(VERDICTS))],
        ])
        return head + "\n".join(body) + "\n"

    parts = [head]
    if mode == "structured":
        parts.append("**【一句话总结】**\n%s\n" % _story_one_liner_text(story, stats))
        parts.append("**【时间线】**\n%s" % _story_timeline_table(timeline))
        parts.append("**【关键人物与立场】**\n%s" % _story_roles_table(story))
        parts.append("**【核心争议点】**\n%s" % _story_conflicts_text(pack))
        parts.append("**【网友阵营分布】**\n_本地规则不做比例推断；见各贴阵营表。_\n")
        parts.append("**【证据强弱与待核实】**\n%s" % _story_evidence_text(pack))
        parts.append("**【一句话锐评】**\n> %s\n" % VERDICTS[
            util.stable_hash(story.get("id") or "", len(VERDICTS))])
        return "\n".join(parts)

    parts.append("## 一句话吃瓜\n%s\n" % _story_one_liner_text(story, stats))
    parts.append("## 多贴时间线\n%s" % _story_timeline_table(timeline))
    parts.append("## 各方角色\n%s" % _story_roles_table(story))
    parts.append("## 图片证据\n%s" % _story_media_table(pack.get("media_items") or []))
    parts.append("## 争议焦点\n%s" % _story_conflicts_text(pack))
    parts.append("## 证据强弱\n%s" % _story_evidence_text(pack))
    parts.append("## 一句话锐评\n> %s\n" % VERDICTS[
        util.stable_hash(story.get("id") or "", len(VERDICTS))])
    return "\n".join(parts)


def generate_story(db, story_id: str, mode: str = "daily", provider: str = "auto",
                   neutral: bool = False, extra_note: str = "", progress=None) -> dict:
    story = db.get_story(story_id)
    if not story:
        raise ValueError("事件不存在")
    pack = _story_pack(db, story)
    stats = linker.story_stats(db, story_id)
    timeline = linker.story_timeline(db, story_id, limit=80)
    labels = pack["labels"]
    pack["media_items"] = _story_media_rows(db, story, labels)
    pack["thread_analysis"] = [
        {"label": labels[int(thread["tid"])],
         "analysis": analyze(db.grouped_floors(int(thread["tid"])))}
        for thread in story["threads"]
    ]

    cfg = llm.resolve(db)
    use_llm = provider not in ("local", "offline") and (
        (cfg.get("type") == "cli" and llm.cli_available(cfg.get("command")))
        or (cfg.get("type") not in (None, "", "none", "cli") and cfg.get("api_key"))
    )
    llm_meta, error, engine = {}, "", "本地规则引擎"
    started = time.time()
    if use_llm:
        prompt = prompts.build_story_prompt(mode, story, story["threads"],
                                            pack["floors"], pack["media"],
                                            pack["stats"], neutral, extra_note)
        if progress:
            progress("多贴材料 %d 字，呼叫模型…" % prompt["chars"])
        try:
            text, llm_meta = llm.chat(cfg, prompt["system"], prompt["user"])
            markdown = _strip_wrapper(text) + "\n\n---\n\n_合并 %d 贴 ｜ 模型：%s_" % (
                len(story["threads"]), llm_meta.get("model") or cfg.get("model"))
            engine = "大模型：%s" % (llm_meta.get("model") or cfg.get("model"))
        except Exception as exc:
            error = str(exc)
            markdown = offline_story_report(mode, story, pack, stats, timeline)
            markdown += "\n\n> ⚠️ 模型调用失败，已退回本地规则报告：%s\n" % util.clip(error, 160)
            engine = "本地兜底（模型失败）"
    else:
        markdown = offline_story_report(mode, story, pack, stats, timeline)
        if provider == "auto":
            error = "未配置模型，使用本地规则引擎"

    report = {
        "tid": 0, "story_id": story_id, "scope": "story", "mode": mode,
        "provider": "llm" if use_llm and not error else "local",
        "model": llm_meta.get("model") or (cfg.get("model") if use_llm else "规则引擎"),
        "title": "事件 · %s" % (story.get("title") or "多贴事件"),
        "markdown": markdown,
        "meta": {
            "engine": engine,
            "mode_name": prompts.MODES.get(mode, prompts.MODES["daily"])["name"],
            "threads": len(story["threads"]), "floors": stats.get("floors") or 0,
            "participants": stats.get("participants") or 0,
            "span": stats.get("span") or 0, "neutral": bool(neutral),
            "chars": len(markdown), "seconds": round(time.time() - started, 1),
            "error": error, "thread_title": story.get("title"),
            "forum": story.get("forum"), "author": "",
            "first_tid": (story["threads"][0]["tid"] if story.get("threads") else 0),
            "media": len(pack["media_items"]),
        },
    }
    report["id"] = db.save_report(report)
    return report
