#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gossip import render  # noqa: E402


class MarkdownTest(unittest.TestCase):
    def test_basic_blocks(self):
        html = render.markdown_to_html(
            "# 标题\n\n正文 **加粗** 和 `代码`\n\n- 一\n- 二\n\n> 引用\n\n---\n")
        self.assertIn("<h1>标题</h1>", html)
        self.assertIn("<strong>加粗</strong>", html)
        self.assertIn("<code>代码</code>", html)
        self.assertIn("<ul>", html)
        self.assertIn("<blockquote>", html)
        self.assertIn("<hr>", html)

    def test_table(self):
        html = render.markdown_to_html("| 时间 | 现场 |\n| --- | --- |\n| 11-02 | 开楼 |")
        self.assertIn("<table>", html)
        self.assertIn("<th>时间</th>", html)
        self.assertIn("<td>开楼</td>", html)

    def test_escapes_html(self):
        html = render.markdown_to_html("<script>alert(1)</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_ordered_list(self):
        html = render.markdown_to_html("1. 第一\n2. 第二")
        self.assertIn("<ol>", html)
        self.assertIn("<li>第一</li>", html)


class ReportPageTest(unittest.TestCase):
    def test_page_contains_cover_and_body(self):
        report = {"id": "abc123", "tid": 42, "title": "吃瓜日报 · 测试",
                  "mode": "daily", "provider": "local", "created_at": 1793500000,
                  "markdown": "## 一句话吃瓜\n楼主在直播面基。\n\n> 引用",
                  "meta": {"floors": 12, "participants": 9, "span": 3600,
                           "mode_name": "吃瓜日报", "engine": "本地规则",
                           "thread_title": "面基翻车", "forum": "憨批"}}
        page = render.report_page(report, {"tid": 42})
        self.assertIn("吃瓜日报 · 测试", page)
        self.assertIn("保存长图", page)
        self.assertIn("data:image/svg+xml;base64,", page)
        self.assertIn("<h2>一句话吃瓜</h2>", page)
        self.assertIn("12 楼", page)

    def test_cover_svg_is_valid(self):
        svg = render.cover_svg("测试标题", "憨批 · 12 楼", "2026.11.03", 2)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("吃瓜日报", svg)
        self.assertLess(len(svg), 12000)


if __name__ == "__main__":
    unittest.main()
