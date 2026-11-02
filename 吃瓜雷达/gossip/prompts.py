#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四种切瓜提示词 + 材料拼装。

模式来自用户给的四套模板，这里做成可执行版本：
把模板原文、硬性规则、帖子材料拼成一次请求，材料按预算裁剪。
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from . import util

MODES: Dict[str, dict] = {
    "daily": {
        "name": "吃瓜日报",
        "icon": "🍉",
        "tagline": "通用版：时间轴 + 阵营 + 神回复",
        "budget": 14000,
        "task": (
            "你是一个专业的吃瓜群众，也是一个逻辑清晰的吃瓜总结员。"
            "请阅读下面的贴吧帖子材料（含楼层、用户名、时间），梳理事件的起因、经过和结果。\n"
            "重点关注四件事：\n"
            "1. 楼主的核心观点或遭遇；\n"
            "2. 主要的争议点是什么；\n"
            "3. 关键的「神回复」或反转楼层；\n"
            "4. 网友们的主要阵营和观点分布。\n"
            "请用幽默风趣但逻辑严密的语言，输出一份《吃瓜日报》，"
            "必须包含事件发生的完整时间轴，并给出「一句话吃瓜」开头。"
        ),
        "outline": (
            "## 一句话吃瓜\n"
            "## 时间轴\n（用表格：时间 / 现场发生了什么 / 出处 #楼层 @用户）\n"
            "## 楼主到底在经历什么\n"
            "## 争议焦点\n（1、2、3 分条，每条写清双方说法）\n"
            "## 神回复 / 反转位\n（引用原文 + 楼层号）\n"
            "## 阵营分布\n（支持方 / 反对方 / 中间派 / 乐子人，估算占比并给代表观点）\n"
            "## 证据强弱\n（已确认 / 单方说法 / 待核实）\n"
            "## 一句话锐评"
        ),
    },
    "structured": {
        "name": "结构化吃瓜",
        "icon": "🧊",
        "tagline": "专业版：时间线 + 立场 + 证据强弱",
        "budget": 13000,
        "task": (
            "请帮我完整吃下这个瓜，按固定结构输出，不要漏项，不要自己加章节。"
        ),
        "outline": (
            "【一句话总结】\n用 2-3 句话说清楚这瓜是什么、走到哪了、关键在哪。\n\n"
            "【时间线】\n按时间顺序梳理关键节点（尽量精确到日期/小时），每个节点注明来源楼层。\n\n"
            "【关键人物与立场】\n列出主要当事人，标注各自角色（楼主/当事人/围观者/爆料人）和目前态度。\n\n"
            "【核心争议点】\n明确双方/多方在吵什么，一条一句。\n\n"
            "【网友阵营分布】\n支持方、反对方、中间派的主要观点，可概括比例或倾向。\n\n"
            "【证据强弱与待核实】\n哪些是已确认事实（楼层原文可查），哪些只是单方面说法/截图/传闻。\n\n"
            "【一句话锐评】\n克制一点，不要人身攻击。"
        ),
    },
    "review": {
        "name": "热点复盘",
        "icon": "🔁",
        "tagline": "长线瓜：全景 + 转折点 + 待观察",
        "budget": 16000,
        "task": (
            "请对下面这个仍在发酵的事件做一次深度复盘。"
            "重点是「结构」而不是「热闹」：谁在推动、哪里转折、哪些说法还没落地。"
        ),
        "outline": (
            "## 事件时间线全景图\n"
            "1. 时间范围界定（从第一楼到最新一楼）\n"
            "2. 关键节点梳理（时间 + 事件描述 + 参与主体 + 楼层号）\n"
            "3. 标注转折点、舆论高峰、反转点（没有就写「暂未出现」）\n\n"
            "## 多维信息汇总\n"
            "1. 楼主/当事人的公开表态（引用原话）\n"
            "2. 各方核心观点对照（表格）\n"
            "3. 网友主要阵营与情绪分布（含情绪词、典型句式）\n\n"
            "## 争议焦点与事实核查\n"
            "列表标注：已确认 / 说法冲突 / 尚未证实\n\n"
            "## 当前状态 + 待观察点\n"
            "现在停在哪一步，接下来盯什么。"
        ),
    },
    "quick": {
        "name": "极简速吃",
        "icon": "⚡",
        "tagline": "30 秒看懂：五句话讲完",
        "budget": 8000,
        "task": "帮我快速吃完这个瓜，严格按 5 条输出，每条不超过 2 行。",
        "outline": (
            "1. 一句话概括\n"
            "2. 时间线（简版，3-5 个节点）\n"
            "3. 谁对谁、吵什么\n"
            "4. 目前站队情况（含大致比例）\n"
            "5. 有没有反转或神回复（没有就写「目前没有硬反转」）"
        ),
    },
}

DEFAULT_MODE = "daily"

RULES = """【硬性要求】
1. 只使用材料里出现的信息。人名、时间、金额、楼层号一律不得编造；材料没写的，就写「材料未提及」。
2. 每个关键结论后面用 `#楼层号 @用户名` 标注出处，方便回帖核对。
3. 只有一方说法、没有旁证的内容，标注「（单方说法，待核实）」。截图、传闻、听说，标注「（未证实）」。
4. 不要给当事人做道德定性，不要人身攻击，不要煽动网暴；吐槽可以，冲着「剧情」去，不要冲着「人」去。
5. 直接输出 Markdown 正文，不要写「好的」「以下是」这类开场白，不要解释你的分析过程。
6. 材料可能不完整（贴吧会抽楼、删楼），结论要留余地，别把没看到的东西当成没发生。
"""

NEUTRAL_APPENDIX = (
    "7. 本次是中立模式：不要站队，只陈述事实、各方观点和证据强弱；"
    "可以指出逻辑漏洞，但不要下「谁对谁错」的判决。\n"
)


def mode_meta() -> List[dict]:
    return [
        {
            "key": key,
            "name": mode["name"],
            "icon": mode["icon"],
            "tagline": mode["tagline"],
            "budget": mode["budget"],
        }
        for key, mode in MODES.items()
    ]


def format_stats(thread: dict, stats: dict, floor_total: int) -> str:
    span = util.human_span(stats.get("span") or 0)
    lines = [
        "【帖子】%s" % (thread.get("title") or "（无标题）"),
        "tid %s ｜ 吧：%s ｜ 楼主：%s ｜ 发帖：%s" % (
            thread.get("tid"), thread.get("forum") or "未知",
            thread.get("author") or "未知", util.fmt_ts(thread.get("create_ts"), "%Y-%m-%d %H:%M") or "未知"),
        "主楼层 %d ｜ 参与人数 %d ｜ 楼主发言 %d ｜ 时间跨度 %s" % (
            stats.get("floors") or floor_total, stats.get("participants") or 0,
            stats.get("op_floors") or 0, span or "未知"),
        "材料抓取于 %s，之后可能还有新楼层。" % util.fmt_ts(time.time(), "%Y-%m-%d %H:%M"),
    ]
    series = stats.get("series") or []
    if series:
        peak = max(series, key=lambda x: x["count"])
        lines.append("讨论高峰：%s（%d 楼）" % (peak["label"], peak["count"]))
    return "\n".join(lines)


def format_floors(floors: List[dict], with_subs: bool = True) -> str:
    chunks = []
    for floor in floors:
        head = "#%s @%s%s %s" % (
            floor.get("floor"),
            floor.get("user_name") or "匿名",
            "（楼主）" if floor.get("is_op") else "",
            util.fmt_ts(floor.get("ts"), "%m-%d %H:%M"),
        )
        extras = []
        if floor.get("likes"):
            extras.append("赞%d" % floor["likes"])
        if floor.get("sub_count"):
            extras.append("楼中楼%d" % floor["sub_count"])
        if extras:
            head += "（%s）" % "，".join(extras)
        body = util.clip(floor.get("content") or "（空楼层/仅图片）", 600)
        chunks.append("%s\n%s" % (head, body))
        subs = floor.get("subs") or []
        if with_subs and subs:
            for sub in subs[:3]:
                chunks.append("  ↳ @%s：%s" % (
                    sub.get("user_name") or "匿名",
                    util.clip(sub.get("content") or "", 160)))
    return "\n\n".join(chunks)


def build_prompt(mode_key: str, thread: dict, stats: dict,
                 floors: List[dict], floor_total: int,
                 neutral: bool = False, extra_note: str = "") -> Dict[str, str]:
    mode = MODES.get(mode_key) or MODES[DEFAULT_MODE]
    rules = RULES + (NEUTRAL_APPENDIX if neutral else "")
    note = ("【额外要求】%s\n" % extra_note.strip()) if extra_note.strip() else ""
    material = "\n\n".join([
        format_stats(thread, stats, floor_total),
        "【楼层节选】共 %d 条，按时序排列（已跳过纯水楼和重复灌水）" % len(floors),
        format_floors(floors),
    ])
    user = (
        "%s\n\n%s\n【输出结构】\n%s\n%s\n【材料开始】\n%s\n【材料结束】"
        % (mode["task"], rules, mode["outline"], note, material)
    )
    system = (
        "你是一个中文社区观察员，擅长把混乱的楼层整理成有时间线、有出处、"
        "不造谣的吃瓜报告。你只依据给定材料写作，材料不足时明确说不足。"
    )
    return {"system": system, "user": user, "mode": mode_key,
            "mode_name": mode["name"], "chars": len(user)}


STORY_RULES = """【多贴合并规则】
1. 材料来自同一个事件的不同帖子，按 T1/T2/T3 编号。先把人物对齐（谁是当事人、谁是围观者），再按时间合并。
2. 时间线必须跨贴排列，写成「T2#31 女主回应」这种，不要按贴分段写「第一贴…第二贴…」。
3. 每个结论标注出处：`T{n}#{楼层}`；图片标注 `T{n}#{楼层}-图{k}`。
4. 各贴之间说法冲突时，列成「他说 / 她说 / 证据」三列，不要替任何一方下判决。
5. 只有一方说法标「（单方，待核实）」；截图/传闻标「（未证实）」；图片看不清就写「图不清」。
6. 找不到关联证据时，明确写「两贴暂未发现直接关联」，不要硬编因果。
"""


def format_story_thread(label: str, thread: dict, floors: List[dict],
                        media_map: dict, stats: dict) -> str:
    head = "【%s】%s\n楼主：%s ｜ 发帖：%s ｜ %d 楼 / %d 人" % (
        label, thread.get("title") or "无标题", thread.get("author") or "未知",
        util.fmt_ts(thread.get("create_ts"), "%Y-%m-%d %H:%M") or "未知",
        stats.get("floors") or 0, stats.get("participants") or 0)
    chunks = [head]
    for floor in floors:
        key = (int(floor.get("floor") or 0), int(floor.get("sub_id") or 0))
        lines = ["%s#%s @%s%s %s" % (
            label, floor.get("floor"), floor.get("user_name") or "匿名",
            "（楼主）" if floor.get("is_op") else "",
            util.fmt_ts(floor.get("ts"), "%m-%d %H:%M"))]
        lines.append(util.clip(floor.get("content") or "", 500))
        for index, media in enumerate(media_map.get(key, []), 1):
            analysis = media.get("analysis") or {}
            if not analysis:
                continue
            lines.append("  [图%d %s] OCR：%s ｜ 描述：%s" % (
                index, analysis.get("type") or "other",
                util.clip(analysis.get("ocr_text") or "", 300) or "（无文字）",
                util.clip(analysis.get("description") or "", 120)))
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks)


def build_story_prompt(mode_key: str, story: dict, threads: List[dict],
                       floors_by_tid: Dict[int, List[dict]],
                       media_by_tid: Dict[int, dict],
                       stats_by_tid: Dict[int, dict],
                       neutral: bool = False, extra_note: str = "") -> Dict[str, str]:
    mode = MODES.get(mode_key) or MODES[DEFAULT_MODE]
    labels = {int(t["tid"]): "T%d" % (i + 1) for i, t in enumerate(threads)}
    blocks = []
    for thread in threads:
        tid = int(thread["tid"])
        blocks.append(format_story_thread(labels[tid], thread,
                                          floors_by_tid.get(tid) or [],
                                          media_by_tid.get(tid) or {},
                                          stats_by_tid.get(tid) or {}))
    outline = (
        "## 一句话吃瓜\n"
        "## 多贴时间线\n（表格：时间 / 贴子 / 现场 / 出处 T{n}#楼层）\n"
        "## 各方角色与说法\n"
        "## 争议焦点\n（列出各贴之间的冲突说法）\n"
        "## 图片证据\n（T{n}#楼层-图k 的 OCR 与描述，图片与文字对不上时要点出来）\n"
        "## 反转点\n"
        "## 证据强弱\n"
        "## 一句话锐评"
    )
    rules = RULES + STORY_RULES + (NEUTRAL_APPENDIX if neutral else "")
    note = ("【额外要求】%s\n" % extra_note.strip()) if extra_note.strip() else ""
    user = "%s\n\n%s\n【输出结构】\n%s\n%s\n【材料开始】\n事件：%s\n%s\n【材料结束】" % (
        mode["task"], rules, outline, note, story.get("title") or "多贴事件",
        "\n\n".join(blocks))
    return {"system": "你是中文社区事件复盘员，只依据给定材料写作，擅长跨贴对齐时间线。",
            "user": user, "mode": mode_key, "mode_name": mode["name"],
            "chars": len(user)}


def build_cover_prompt(thread: dict, stats: dict) -> str:
    return (
        "扁平插画风「吃瓜日报」封面，16:9。主题：%s。"
        "画面元素：西瓜切片、放大镜、小喇叭、聊天气泡、时间轴刻度线；"
        "配色：深墨绿底 + 西瓜红点缀，干净留白，中文标题「吃瓜日报」，"
        "副标题「%d 楼 / %d 人围观」。不要真人肖像，不要二维码，不要多余文字。"
        % (util.clip(thread.get("title") or "贴吧吃瓜", 40),
           stats.get("floors") or 0, stats.get("participants") or 0)
    )
