#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把抽奖结果渲染成一张可直接截图发贴的公示页（单文件、无外链）。"""
from __future__ import annotations

import html
from pathlib import Path

CSS = """
*{box-sizing:border-box}
body{margin:0;padding:32px 18px 60px;background:#f5f6f8;color:#1b1d21;
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto}
.card{background:#fff;border-radius:16px;padding:26px 26px 22px;
 box-shadow:0 2px 10px rgba(0,0,0,.06),0 12px 32px rgba(0,0,0,.05);margin-bottom:18px}
h1{margin:0 0 6px;font-size:24px;letter-spacing:.5px}
.sub{color:#8a8f99;font-size:13px;word-break:break-all}
.sub a{color:#8a8f99}
.badge{display:inline-block;background:#e4002b;color:#fff;border-radius:999px;
 padding:2px 12px;font-size:12px;margin-right:8px;vertical-align:middle}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(122px,1fr));gap:12px;margin-top:18px}
.stat{background:#f7f8fa;border:1px solid #eceef2;border-radius:12px;padding:12px 14px}
.stat b{display:block;font-size:23px;line-height:1.2;font-variant-numeric:tabular-nums}
.stat span{color:#8a8f99;font-size:12px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.chip{background:#fff2f4;color:#c40027;border:1px solid #ffd3dc;border-radius:8px;
 padding:4px 10px;font-size:13px;font-weight:600}
.chip em{font-style:normal;color:#8a8f99;font-weight:400;margin-left:4px}
h2{font-size:15px;margin:0 0 12px;color:#3a3f48;letter-spacing:.4px}
h2 small{color:#a3a8b2;font-weight:400;font-size:12px;margin-left:6px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 9px;text-align:left;border-bottom:1px solid #f0f1f4;white-space:nowrap}
th{color:#8a8f99;font-weight:500;font-size:11.5px;letter-spacing:.5px}
td.num{width:44px;color:#8a8f99;font-variant-numeric:tabular-nums}
td.floor{width:64px;color:#8a8f99;font-variant-numeric:tabular-nums}
td.name{font-weight:600}
td.nick{color:#8a8f99;font-size:13px}
table.one tbody tr:nth-child(-n+3) td{background:#fffdf3}
table.compact{font-size:13px}
table.compact th,table.compact td{padding:6px 8px}
table.compact td.nick{font-size:12px}
.cols{display:grid;gap:6px 24px;align-items:start}
.cols2{grid-template-columns:repeat(2,minmax(0,1fr))}
.cols3{grid-template-columns:repeat(3,minmax(0,1fr))}
@media (max-width:980px){.cols3{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:660px){.cols3,.cols2{grid-template-columns:minmax(0,1fr)}}
.cols table{min-width:0}
textarea{width:100%;min-height:84px;border:1px solid #e3e5ea;border-radius:10px;padding:10px;
 font:13px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;resize:vertical;background:#fafbfc}
button{background:#e4002b;color:#fff;border:0;border-radius:9px;padding:8px 16px;
 font-size:13px;font-weight:600;cursor:pointer;margin-top:10px}
button:active{transform:translateY(1px)}
.foot{margin-top:6px;color:#9aa0aa;font-size:12px;line-height:2;word-break:break-all}
.foot code{background:#f2f3f6;border-radius:5px;padding:1px 6px;font-size:11.5px}
.note{color:#9aa0aa;font-size:12px;margin-top:10px}
@media (max-width:560px){body{padding:16px 10px 40px}.card{padding:18px 16px}
 h1{font-size:19px}.stat b{font-size:19px}}
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head>
<body><div class="wrap">
<div class="card">
  <h1><span class="badge">公示</span>{title}</h1>
  <div class="sub">{source_line}</div>
  <div class="grid">{stats}</div>
  <div class="chips">{chips}</div>
</div>
<div class="card">
  <h2>中奖名单<small>共 {total} 名</small></h2>
  {table_block}
  {shortfall_note}
</div>
<div class="card">
  <h2>@ 名单<small>点按钮复制，直接粘到贴吧</small></h2>
  <textarea id="at" readonly>{at_list}</textarea>
  <button onclick="cp()">复制 @ 名单</button>
  <div class="note">候补 {backup_count} 人，有人放弃可顺延（明细见 backup.csv）</div>
</div>
<div class="card">
  <h2>可复核凭证</h2>
  <div class="foot">
    随机种子 <code>{seed}</code><br>
    算法 <code>{algorithm}</code><br>
    输入指纹 <code>{input_sha}</code><br>
    结果指纹 <code>{result_sha}</code><br>
    去重依据 <code>{dedupe}</code> · 总回复 {total_replies} 条 → 去重 {unique} 人 · 生成于 {created_at}
  </div>
</div>
</div>
<script>
function cp(){{var t=document.getElementById('at');t.select();
(navigator.clipboard?navigator.clipboard.writeText(t.value):Promise.reject()).catch(function(){{
try{{document.execCommand('copy')}}catch(e){{}}}});}}
</script></body></html>
"""

HEAD = ('<table class="{cls}"><thead><tr><th>序号</th>{prize_col}'
        '<th>楼层</th><th>用户名</th><th>昵称</th></tr></thead><tbody>')


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _row(w: dict, with_prize: bool) -> str:
    prize = '<td>%s</td>' % _e(w.get("prize")) if with_prize else ""
    return ('<tr><td class="num">%d</td>%s<td class="floor">%s楼</td>'
            '<td class="name">%s</td><td class="nick">%s</td></tr>'
            % (int(w.get("seq") or 0), prize, _e(w.get("floor")),
               _e(w.get("user_name") or w.get("nickname")), _e(w.get("nickname"))))


def _tables(winners, cols: int, with_prize: bool) -> str:
    if not winners:
        return ('<table><thead><tr><th>序号</th><th>楼层</th><th>用户名</th><th>昵称</th>'
                '</tr></thead><tbody><tr><td colspan="4">没有中奖者</td></tr></tbody></table>')
    prize_col = "<th>奖品</th>" if with_prize else ""
    if cols <= 1:
        return (HEAD.format(cls="one", prize_col=prize_col)
                + "".join(_row(w, with_prize) for w in winners) + "</tbody></table>")
    per = (len(winners) + cols - 1) // cols
    parts = []
    for c in range(cols):
        chunk = winners[c * per:(c + 1) * per]
        if not chunk:
            continue
        parts.append(HEAD.format(cls="compact", prize_col=prize_col)
                     + "".join(_row(w, with_prize) for w in chunk) + "</tbody></table>")
    return '<div class="cols cols%d">%s</div>' % (cols if cols <= 3 else 3, "".join(parts))


def render(proof: dict, out_path: Path) -> Path:
    stats = proof.get("stats") or {}
    winners = proof.get("winners") or []
    prizes = proof.get("prizes") or []
    tid = proof.get("tid") or 0
    total = len(winners)

    if tid:
        source_line = '帖子 <a href="https://tieba.baidu.com/p/%s" target="_blank">tieba.baidu.com/p/%s</a>' % (
            _e(tid), _e(tid))
    else:
        source_line = "数据来源 %s" % _e(proof.get("source") or "-")

    cards = [
        (stats.get("total_replies", 0), "总回复"),
        (stats.get("unique_users", 0), "去重后参与"),
        (total, "中奖人数"),
        (len(proof.get("backup") or []), "候补"),
        (stats.get("duplicate_hits", 0), "重复回复"),
    ]
    stats_html = "".join('<div class="stat"><b>%s</b><span>%s</span></div>' % (_e(n), _e(l))
                         for n, l in cards)
    chips = "".join('<div class="chip">%s<em>× %d</em></div>' % (_e(p["name"]), int(p["count"]))
                    for p in prizes)

    single_prize = len(prizes) <= 1
    cols = 1 if total <= 24 else (2 if total <= 60 else 3)
    table_block = _tables(winners, cols, with_prize=not single_prize)

    shortfall = int(proof.get("shortfall") or 0)
    shortfall_note = ('<div class="note">注意：参与人数不足，有 %d 份奖品未抽出。</div>' % shortfall
                      if shortfall else "")

    at_list = " ".join("@" + (w.get("user_name") or w.get("nickname") or "")
                       for w in winners if (w.get("user_name") or w.get("nickname")))
    keymix = stats.get("keymix") or {}
    dedupe = ", ".join("%s×%d" % (k, v) for k, v in sorted(keymix.items())) or "-"

    page = TEMPLATE.format(
        title=_e("贴吧抽奖中奖公示"), css=CSS, source_line=source_line, stats=stats_html,
        chips=chips, total=total, table_block=table_block, shortfall_note=shortfall_note,
        at_list=_e(at_list), backup_count=len(proof.get("backup") or []),
        seed=_e(proof.get("seed")), algorithm=_e(proof.get("algorithm")),
        input_sha=_e(proof.get("input_sha256")), result_sha=_e(proof.get("result_sha256")),
        dedupe=_e(dedupe), total_replies=stats.get("total_replies", 0),
        unique=stats.get("unique_users", 0), created_at=_e(proof.get("created_at")),
    )
    out_path.write_text(page, encoding="utf-8")
    return out_path
