#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告渲染：Markdown → HTML，以及能直接截图发帖的独立报告页。"""
from __future__ import annotations

import base64
import html
import re
import time
from typing import List, Optional

from . import util


# ------------------------------------------------------------------ Markdown
def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                  r'<a href="\2" target="_blank" rel="noreferrer">\1</a>', text)
    return text


def _is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", line)) and "-" in line


def markdown_to_html(md: str) -> str:
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: List[str] = []
    i = 0
    in_list = False
    list_tag = "ul"

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</%s>" % list_tag)
            in_list = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            close_list()
            i += 1
            continue

        if stripped.startswith("```"):
            close_list()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>%s</code></pre>" % html.escape("\n".join(code)))
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            out.append("<h%d>%s</h%d>" % (level, _inline(heading.group(2)), level))
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            close_list()
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            close_list()
            header = [c for c in stripped.strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>%s</tr></thead><tbody>" % "".join(
                "<th>%s</th>" % _inline(c.strip()) for c in header))
            for row in rows:
                out.append("<tr>%s</tr>" % "".join(
                    "<td>%s</td>" % _inline(c.strip()) for c in row))
            out.append("</tbody></table>")
            continue

        if stripped.startswith(">"):
            close_list()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>"
                       % markdown_to_html("\n".join(quote)))
            continue

        ordered = re.match(r"^\s*(\d+)[.、)]\s+(.*)$", line)
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if ordered or bullet:
            tag = "ol" if ordered else "ul"
            if not in_list or list_tag != tag:
                close_list()
                list_tag = tag
                out.append("<%s>" % tag)
                in_list = True
            content = ordered.group(2) if ordered else bullet.group(1)
            out.append("<li>%s</li>" % _inline(content))
            i += 1
            continue

        close_list()
        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith(("#", ">", "|", "```"))
                    or re.match(r"^(-{3,}|\*{3,})$", nxt)
                    or re.match(r"^\s*(\d+)[.、)]\s+", lines[i])
                    or re.match(r"^\s*[-*+]\s+", lines[i])):
                break
            para.append(nxt)
            i += 1
        out.append("<p>%s</p>" % "<br>".join(_inline(p) for p in para))
    close_list()
    return "\n".join(out)


# ------------------------------------------------------------------ 封面图
def cover_svg(title: str, subtitle: str, date_text: str, seed: int = 0) -> str:
    palettes = [
        ("#fdfaf8", "#f8e6ed", "#bd416e"),
        ("#fdfaf8", "#f6eee2", "#c98a2e"),
        ("#fbf7f8", "#f4e9e4", "#d66558"),
        ("#fbf7f8", "#f1e8ef", "#8f5c6e"),
    ]
    bg, bg2, accent = palettes[abs(seed) % len(palettes)]
    title = html.escape(util.clip(title, 30))
    subtitle = html.escape(util.clip(subtitle, 48))
    grid = "\n  ".join(
        '<line x1="%d" y1="0" x2="%d" y2="630"/>' % (x, x + 170)
        for x in range(-100, 1300, 170))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="{bg}"/><stop offset="100%" stop-color="{bg2}"/>
  </linearGradient>
  <radialGradient id="glow" cx="0.82" cy="0.16" r="0.66">
    <stop offset="0%" stop-color="{accent}" stop-opacity="0.20"/>
    <stop offset="100%" stop-color="{accent}" stop-opacity="0"/>
  </radialGradient>
</defs>
<rect width="1200" height="630" fill="url(#bg)"/>
<rect width="1200" height="630" fill="url(#glow)"/>
<g opacity="0.07" stroke="{accent}" stroke-width="1">
  {grid}
</g>
<g transform="translate(88,96)">
  <text x="0" y="0" font-family="PingFang SC, Noto Sans SC, sans-serif" font-size="24"
        fill="{accent}" letter-spacing="7">GUA · DAILY</text>
  <text x="0" y="94" font-family="Songti SC, Noto Serif SC, serif" font-size="62"
        font-weight="500" fill="#211419">吃瓜日报</text>
  <text x="0" y="148" font-family="PingFang SC, Noto Sans SC, sans-serif" font-size="21"
        fill="#6b515b">{date_text}</text>
  <text x="0" y="312" font-family="Songti SC, Noto Serif SC, serif" font-size="46"
        font-weight="500" fill="#211419">{title}</text>
  <text x="0" y="372" font-family="PingFang SC, Noto Sans SC, sans-serif" font-size="23"
        fill="#59424b">{subtitle}</text>
  <text x="0" y="458" font-family="PingFang SC, Noto Sans SC, sans-serif" font-size="18"
        fill="{accent}">贴吧 · 楼层实况 · 自动切瓜</text>
</g>
<g transform="translate(978,392)">
  <circle r="118" fill="{accent}" fill-opacity="0.07"/>
  <path d="M-96,30 A96,96 0 0 0 96,30 Z" fill="#2f986f"/>
  <path d="M-84,30 A84,84 0 0 0 84,30 Z" fill="#e0567f"/>
  <path d="M-84,30 A84,84 0 0 0 84,30" fill="none" stroke="#fdfaf8" stroke-opacity="0.7" stroke-width="3"/>
  <ellipse cx="-36" cy="52" rx="5" ry="7.5" fill="#5c2a3a" opacity="0.55"/>
  <ellipse cx="0" cy="66" rx="5" ry="7.5" fill="#5c2a3a" opacity="0.55"/>
  <ellipse cx="36" cy="52" rx="5" ry="7.5" fill="#5c2a3a" opacity="0.55"/>
</g>
</svg>"""


def cover_data_uri(svg: str) -> str:
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return "data:image/svg+xml;base64," + encoded


# ------------------------------------------------------------------ 报告页
REPORT_CSS = """
:root{
  --ink:#211419;--sub:#59424b;--line:rgba(96,62,74,.12);--accent:#bd416e;
  --paper:#ffffff;--canvas:#fdfaf8;--soft:#f9f2f5;--berry-soft:#fff0f5;
  --serif:"Songti SC","SimSun","STSong","Noto Serif CJK SC","Noto Serif SC","Cormorant Garamond",serif;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Display","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--canvas);color:var(--ink);font-family:var(--sans);
 line-height:1.78;-webkit-font-smoothing:antialiased}
.wrap{max-width:900px;margin:0 auto;padding:28px 18px 60px}
.paper{background:var(--paper);border-radius:24px;overflow:hidden;
 box-shadow:0 24px 60px -22px rgba(58,36,46,.18),0 6px 16px rgba(58,36,46,.05);
 border:1px solid var(--line)}
.cover{display:block;width:100%;height:auto}
.masthead{display:flex;justify-content:space-between;align-items:baseline;
 padding:22px 44px 0;color:var(--sub);font-size:12px;letter-spacing:.6px}
.masthead b{color:var(--accent);letter-spacing:2.4px;font-size:12px;font-weight:600}
.body{padding:10px 44px 44px}
h1{font-family:var(--serif);font-weight:500;font-size:32px;line-height:1.35;margin:14px 0 6px;
 letter-spacing:-.3px;color:var(--ink)}
h2{font-family:var(--sans);font-size:20px;margin:36px 0 10px;padding-left:12px;
 border-left:4px solid var(--accent);line-height:1.4;color:var(--ink)}
h3{font-size:16.5px;margin:22px 0 8px;color:var(--ink)}
p{margin:10px 0;font-size:15.5px;color:var(--ink)}
ul,ol{margin:10px 0;padding-left:24px}
li{margin:6px 0;font-size:15px;color:var(--ink)}
blockquote{margin:14px 0;padding:12px 18px;background:#fbf7f8;
 border-left:3px solid rgba(189,65,110,.35);border-radius:0 10px 10px 0;
 color:var(--sub);font-size:14.8px}
blockquote blockquote{background:transparent;border-left:2px solid var(--line);margin:8px 0}
table{width:100%;border-collapse:collapse;margin:16px 0;font-size:13.8px}
th,td{border:1px solid var(--line);padding:9px 11px;text-align:left;vertical-align:top}
td:first-child,th:first-child{white-space:nowrap}
th{background:var(--soft);font-weight:600;white-space:nowrap;color:var(--ink)}
tr:nth-child(even) td{background:#fdfaf8}
code{background:var(--soft);border:1px solid var(--line);padding:1px 5px;border-radius:5px;
 font-size:12.5px;font-family:"SF Mono",Menlo,monospace}
hr{border:0;border-top:1px dashed var(--line);margin:30px 0}
a{color:#a5335e}
.meta{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 4px}
.chip{font-size:12px;color:var(--sub);background:var(--soft);border:1px solid var(--line);
 border-radius:999px;padding:4px 11px}
.foot{margin-top:30px;padding-top:16px;border-top:1px solid var(--line);
 font-size:12.2px;color:var(--sub);display:flex;justify-content:space-between;gap:12px}
.tools{position:fixed;right:22px;bottom:22px;display:flex;flex-direction:column;gap:8px;z-index:9}
.tools button{border:0;border-radius:999px;padding:10px 18px;background:#211419;color:#fbf6f3;
 font-size:12.5px;cursor:pointer;box-shadow:0 14px 30px -10px rgba(58,36,46,.4);
 font-family:var(--sans)}
.tools button.ghost{background:#ffffff;color:#211419;border:1px solid var(--line);
 box-shadow:0 6px 16px rgba(58,36,46,.08)}
@media (max-width:640px){
 body{line-height:1.7}
 .tools{left:10px;right:10px;bottom:10px;flex-direction:row;justify-content:center;gap:6px}
 .tools button{padding:8px 13px;font-size:11.5px}
 table{font-size:12.8px}
 .wrap{padding:14px 10px 40px}
 .paper{border-radius:18px}
 .body{padding:6px 18px 26px}
 .masthead{padding:16px 18px 0}
 h1{font-size:24px}
 h2{font-size:18px;margin-top:28px}
 table{display:block;overflow-x:auto;white-space:nowrap}
 .foot{flex-direction:column;gap:6px}
}
@media print{
 body{background:#fff}.tools{display:none}.paper{box-shadow:none;border:0;border-radius:0}
 .wrap{max-width:none;padding:0}.body{padding:0 6mm}
}
"""

REPORT_JS = """
function saveLongImage(){
  try{
    var node=document.querySelector('.paper');
    var w=node.offsetWidth, h=node.offsetHeight;
    var css=Array.prototype.map.call(document.styleSheets,function(s){
      try{return Array.prototype.map.call(s.cssRules,function(r){return r.cssText}).join('\\n')}
      catch(e){return ''}
    }).join('\\n');
    var clone=node.cloneNode(true);
    var holder=document.createElement('div');
    holder.setAttribute('xmlns','http://www.w3.org/1999/xhtml');
    holder.style.width=w+'px';
    var style=document.createElement('style');
    style.textContent=css+' .tools{display:none!important}';
    holder.appendChild(style); holder.appendChild(clone);
    var svg='<svg xmlns="http://www.w3.org/2000/svg" width="'+w+'" height="'+h+'">'
      +'<foreignObject width="100%" height="100%">'
      +new XMLSerializer().serializeToString(holder)+'</foreignObject></svg>';
    var img=new Image();
    img.onload=function(){
      var scale=2, canvas=document.createElement('canvas');
      canvas.width=w*scale; canvas.height=h*scale;
      var ctx=canvas.getContext('2d'); ctx.scale(scale,scale);
      ctx.fillStyle='#eef1ee'; ctx.fillRect(0,0,w,h); ctx.drawImage(img,0,0);
      canvas.toBlob(function(blob){
        var a=document.createElement('a');
        a.href=URL.createObjectURL(blob); a.download='guatan-report.png'; a.click();
        setTimeout(function(){URL.revokeObjectURL(a.href)},4000);
      },'image/png');
    };
    img.onerror=function(){alert('长图导出失败，可以直接截图或打印成 PDF');};
    img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);
  }catch(e){alert('长图导出失败：'+e.message);}
}
function copyMarkdown(){
  var text=document.getElementById('raw-md').value;
  navigator.clipboard.writeText(text).then(function(){alert('Markdown 已复制')},
    function(){alert('复制失败，请手动选择文本框内容')});
}
"""


def report_page(report: dict, thread: Optional[dict] = None,
                cover_url: str = "") -> str:
    meta = report.get("meta") or {}
    md = report.get("markdown") or ""
    body_md = re.sub(r"^\s*#\s+[^\n]*\n+", "", md, count=1) if md.lstrip().startswith("#") else md
    body = markdown_to_html(body_md)
    title = report.get("title") or "吃瓜报告"
    display_title = meta.get("thread_title") or title
    thread = thread or {}
    cover = cover_url
    if not cover:
        svg = cover_svg(meta.get("thread_title") or title,
                        "%s · %d 楼 / %d 人" % (
                            meta.get("forum") or "贴吧",
                            meta.get("floors") or 0, meta.get("participants") or 0),
                        time.strftime("%Y.%m.%d", time.localtime(report.get("created_at") or time.time())),
                        util.stable_hash(str(report.get("id")), 4))
        cover = cover_data_uri(svg)
    scope = report.get("scope") or "thread"
    chips = [
        ("%d 贴" % (meta.get("threads") or 0)) if scope == "story"
        else ("tid %s" % report.get("tid")),
        "%d 楼" % (meta.get("floors") or 0),
        "%d 人" % (meta.get("participants") or 0),
        "跨度 %s" % (util.human_span(meta.get("span") or 0) or "未知"),
        meta.get("mode_name") or report.get("mode") or "",
        meta.get("engine") or "",
    ]
    chip_html = "".join('<span class="chip">%s</span>' % html.escape(str(c)) for c in chips if c)
    source_tid = report.get("tid") or meta.get("first_tid") or ""
    src_url = "https://tieba.baidu.com/p/%s" % source_tid if source_tid else "#"
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title>
<style>%(css)s</style></head>
<body>
<div class="wrap"><div class="paper">
<img class="cover" src="%(cover)s" alt="cover">
<div class="masthead"><b>GUA · DAILY</b><span>%(date)s · 第 %(edition)s 期</span></div>
<div class="body">
<h1>%(display_title)s</h1>
<div class="meta">%(chips)s</div>
%(body)s
<div class="foot">
  <span>来源：<a href="%(src)s" target="_blank">%(src)s</a></span>
  <span>自动整理 · 仅供吃瓜参考 · 不构成事实认定</span>
</div>
</div></div></div>
<div class="tools">
  <button onclick="saveLongImage()">保存长图</button>
  <button class="ghost" onclick="copyMarkdown()">复制 Markdown</button>
  <button class="ghost" onclick="window.print()">打印 / PDF</button>
</div>
<textarea id="raw-md" style="position:fixed;left:-9999px;top:0">%(raw)s</textarea>
<script>%(js)s</script>
</body></html>""" % {
        "title": html.escape(title),
        "display_title": html.escape(display_title),
        "css": REPORT_CSS,
        "cover": cover,
        "date": time.strftime("%Y.%m.%d %H:%M", time.localtime(report.get("created_at") or time.time())),
        "edition": str(util.stable_hash(str(report.get("id")), 900) + 100),
        "chips": chip_html,
        "body": body,
        "src": src_url,
        "raw": html.escape(md),
        "js": REPORT_JS,
    }
