#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gossip import db, summarize  # noqa: E402


def F(no, user, text, ts, op=False, likes=0, subs=None):
    return {"floor": no, "sub_id": 0, "user_id": sum(ord(c) for c in user), "user_name": user,
            "content": text, "ts": ts, "is_op": op, "likes": likes,
            "sub_count": len(subs or []), "images": 0,
            "subs": [{"floor": no, "sub_id": 1000 + i, "user_name": "路人%d" % i,
                      "content": s, "ts": ts, "is_op": False} for i, s in enumerate(subs or [])]}


FLOORS = [
    F(1, "蛋芽", "直播一下和自称C8男的面基，他今天约我在国贸见面，说自己是做投行的。", 1793500000, op=True),
    F(2, "围观A", "蹲，记得更新", 1793500300),
    F(3, "路人B", "笑死，投行男一般不会自称C8", 1793500400, likes=3),
    F(4, "蛋芽", "他到了，穿得像卖保险的，还带了个假劳力士。", 1793500800, op=True),
    F(5, "吃瓜C", "有没有截图？没图说个锤子", 1793500900),
    F(6, "对线D", "凭什么说人家假表？你自己不也背个高仿包", 1793501000),
    F(7, "围观E", "听说他是某平台的运营，同事说他经常这样约人", 1793501200),
    F(8, "蛋芽", "我报警了，警察来了做了笔录。", 1793502000, op=True),
    F(9, "路人F", "支持楼主，安全第一", 1793502100),
    F(10, "吃瓜G", "反转了？前面不是说只是吃饭吗", 1793502600),
    F(11, "对线D", "别带节奏了，人家已经道歉了", 1793502800),
    F(12, "围观H", "等一个官方通报，先别站队", 1793503400, subs=["+1", "让子弹飞"]),
]


class AnalyzeTest(unittest.TestCase):
    def test_stats(self):
        stats = summarize.compute_stats(FLOORS)
        self.assertEqual(stats["floors"], 12)
        self.assertEqual(stats["op_floors"], 3)
        self.assertEqual(stats["participants"], 9)
        self.assertGreater(stats["span"], 0)

    def test_water_and_score(self):
        self.assertTrue(summarize.is_water("顶"))
        self.assertTrue(summarize.is_water("哈哈哈"))
        good = summarize.score_floor(FLOORS[5])
        bad = summarize.score_floor(FLOORS[1])
        self.assertGreater(good, bad)

    def test_select_floors_respects_budget(self):
        picked = summarize.select_floors(FLOORS, budget_chars=400)
        self.assertIn(1, [f["floor"] for f in picked])
        self.assertLess(len(picked), len(FLOORS))

    def test_analysis_shape(self):
        analysis = summarize.analyze(FLOORS)
        self.assertTrue(analysis["timeline"])
        self.assertTrue(analysis["quotes"])
        self.assertIn("开撕", analysis["camps"])
        self.assertIsNotNone(analysis["peak"])

    def test_offline_modes(self):
        thread = {"tid": 1, "title": "面基翻车实录", "author": "蛋芽",
                  "forum": "憨批", "mode_name": "吃瓜日报"}
        checks = {
            "daily": ["## 时间轴", "## 阵营分布", "## 证据强弱", "一句话吃瓜"],
            "structured": ["【一句话总结】", "【时间线】", "【核心争议点】", "【网友阵营分布】"],
            "review": ["## 事件时间线全景图", "## 多维信息汇总", "## 争议焦点与事实核查"],
            "quick": ["1. 一句话概括", "5. 反转或神回复"],
        }
        for mode, needles in checks.items():
            text = summarize.offline_report(mode, thread, FLOORS)
            for needle in needles:
                self.assertIn(needle, text, "%s 缺少 %s" % (mode, needle))
            self.assertIn("本地规则", text)

    def test_timeline_has_source(self):
        analysis = summarize.analyze(FLOORS)
        for row in analysis["timeline"]:
            self.assertTrue(row["source"].startswith("#"))
            self.assertTrue(row["time"])


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = db.Db(os.path.join(self.tmp.name, "t.db"))
        self.db.upsert_thread({"tid": 7, "title": "测试帖", "author": "蛋芽",
                               "author_id": 1, "reply_num": len(FLOORS)})
        self.db.upsert_floors(7, [dict(f, sub_id=0) for f in FLOORS])
        self.db.upsert_floors(7, [s for f in FLOORS for s in
                                  [dict(sub, floor=f["floor"]) for sub in f["subs"]]])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_generate_local(self):
        report = summarize.generate(self.db, 7, mode="daily", provider="local")
        self.assertIn("markdown", report)
        self.assertTrue(report["id"])
        self.assertIn("本地", report["meta"]["engine"])
        stored = self.db.get_report(report["id"])
        self.assertIn("时间轴", stored["markdown"])
        self.assertEqual(len(self.db.list_reports(7)), 1)

    def test_generate_unknown_thread(self):
        with self.assertRaises(ValueError):
            summarize.generate(self.db, 999, provider="local")


if __name__ == "__main__":
    unittest.main()
