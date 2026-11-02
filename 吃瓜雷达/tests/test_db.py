#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gossip import db  # noqa: E402


def floor(no, user, text, ts=1700000000, op=False, sub=0, likes=0):
    return {
        "floor": no, "sub_id": sub, "user_id": abs(hash(user)) % 100000,
        "user": user, "content": text, "ts": ts, "is_op": op,
        "likes": likes, "sub_count": 0, "images": 0,
    }


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = db.Db(os.path.join(self.tmp.name, "test.db"))
        self.db.upsert_thread({"tid": 42, "title": "测试帖", "author": "楼主",
                               "author_id": 1, "reply_num": 10})
        self.db.add_source("thread", "42", title="测试帖", interval=60)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_upsert_new_and_update(self):
        new = self.db.upsert_floors(42, [floor(1, "楼主", "开楼", op=True),
                                        floor(2, "A", "第一")])
        self.assertEqual(len(new), 2)
        again = self.db.upsert_floors(42, [floor(1, "楼主", "开楼（改了）", op=True)])
        self.assertEqual(again, [])
        rows = self.db.list_floor_rows(42)
        self.assertEqual([r["floor"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["content"], "开楼（改了）")

    def test_mark_missing_deleted(self):
        self.db.upsert_floors(42, [floor(1, "楼主", "a", op=True), floor(2, "B", "b")])
        removed = self.db.mark_missing_deleted(42, [(1, 0)])
        self.assertEqual(removed, 1)
        self.assertEqual([r["floor"] for r in self.db.list_floor_rows(42)], [1])

    def test_grouped_floors_attaches_subs(self):
        self.db.upsert_floors(42, [floor(1, "楼主", "开楼", op=True),
                                   floor(2, "B", "二楼"),
                                   floor(2, "C", "楼中楼", sub=99)])
        rows = self.db.grouped_floors(42)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["subs"][0]["content"], "楼中楼")

    def test_filters(self):
        self.db.upsert_floors(42, [
            floor(1, "楼主", "开楼", op=True),
            floor(2, "B", "短"),
            floor(3, "C", "长" * 80),
        ])
        self.assertEqual(len(self.db.grouped_floors(42, mode="op")), 1)
        self.assertEqual(len(self.db.grouped_floors(42, mode="hot")), 1)
        self.assertEqual(len(self.db.grouped_floors(42, query="短")), 1)

    def test_stats(self):
        self.db.upsert_floors(42, [floor(1, "楼主", "a", ts=100, op=True),
                                   floor(2, "B", "b", ts=200),
                                   floor(3, "B", "c", ts=300)])
        stats = self.db.thread_stats(42)
        self.assertEqual(stats["floors"], 3)
        self.assertEqual(stats["participants"], 2)
        self.assertEqual(stats["span"], 200)
        self.assertTrue(stats["series"])

    def test_events_and_config(self):
        self.db.add_event("new_floors", "新增 3 楼", tid=42)
        self.assertEqual(len(self.db.list_events(10)), 1)
        self.db.set_config("monitor", {"max_pages": 12})
        self.assertEqual(self.db.get_config("monitor")["max_pages"], 12)

    def test_source_roundtrip(self):
        sources = self.db.list_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["kind"], "thread")
        self.db.update_source(sources[0]["id"], enabled=0, meta={"a": 1})
        again = self.db.list_sources()[0]
        self.assertEqual(again["enabled"], 0)
        self.assertEqual(again["meta"]["a"], 1)
        self.db.delete_source(sources[0]["id"])
        self.assertEqual(self.db.list_sources(), [])


if __name__ == "__main__":
    unittest.main()
