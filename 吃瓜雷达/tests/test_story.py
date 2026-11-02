#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gossip import db, linker, summarize  # noqa: E402


class StoryReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = db.Db(os.path.join(self.tmp.name, "s.db"))
        for tid, title, ts, body in [
            (11, "直播和C8男面基", 1700000000, "他自称C8，坐公交来"),
            (22, "吧里C8男很火", 1700003600, "C8男太典了"),
        ]:
            op = tid * 10
            self.db.upsert_thread({"tid": tid, "title": title, "author": "楼主%d" % tid,
                                   "author_id": op, "forum": "憨批", "reply_num": 3})
            self.db.upsert_floors(tid, [
                {"floor": 1, "sub_id": 0, "user_id": op, "user": "楼主%d" % tid,
                 "content": body, "ts": ts, "is_op": True},
                {"floor": 2, "sub_id": 0, "user_id": 7, "user": "路人",
                 "content": "笑死 C8", "ts": ts + 60},
                {"floor": 3, "sub_id": 0, "user_id": 8, "user": "围观",
                 "content": "蹲后续", "ts": ts + 90},
            ])
            self.db.refresh_thread_counts(tid)
        self.db.add_media([{"tid": 11, "floor": 1, "sub_id": 0, "pid": 1,
                            "pic_id": "p1", "url": "https://example.com/a.jpg"}])
        media = self.db.media_rows(11)[0]
        self.db.update_media(media["id"], status="done", analysis={
            "engine": "test", "type": "chat", "ocr_text": "14:22 几点到",
            "description": "聊天截图", "entities": ["C8"], "confidence": 0.9})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_story_report_contains_cross_thread(self):
        stories = linker.build_stories(self.db, threshold=0.5)
        self.assertEqual(len(stories), 1)
        report = summarize.generate_story(self.db, stories[0]["id"],
                                          mode="daily", provider="local")
        text = report["markdown"]
        self.assertIn("多贴时间线", text)
        self.assertIn("T1", text)
        self.assertIn("T2", text)
        self.assertIn("图片证据", text)
        self.assertIn("14:22", text)
        self.assertEqual(report["scope"], "story")
        self.assertEqual(self.db.get_report(report["id"])["scope"], "story")

    def test_story_quick_mode(self):
        story = linker.build_stories(self.db, threshold=0.5)[0]
        report = summarize.generate_story(self.db, story["id"], mode="quick",
                                          provider="local")
        self.assertIn("一句话概括", report["markdown"])


if __name__ == "__main__":
    unittest.main()
