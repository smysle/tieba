#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gossip import db, linker  # noqa: E402


def add_thread(database, tid, title, ts, forum="憨批", users=(99, 100),
               body="笑死 这也能叫 C8"):
    op_uid = tid * 10
    database.upsert_thread({"tid": tid, "title": title, "author": "楼主%d" % tid,
                            "author_id": op_uid, "forum": forum, "reply_num": 3})
    database.upsert_floors(tid, [
        {"floor": 1, "sub_id": 0, "user_id": op_uid, "user": "楼主%d" % tid,
         "content": title + " 直播", "ts": ts, "is_op": True},
        {"floor": 2, "sub_id": 0, "user_id": users[0], "user": "路人%d" % users[0],
         "content": body, "ts": ts + 60},
        {"floor": 3, "sub_id": 0, "user_id": users[1], "user": "另一个%d" % users[1],
         "content": "蹲后续 等更新", "ts": ts + 120},
    ])
    database.refresh_thread_counts(tid)


class LinkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = db.Db(os.path.join(self.tmp.name, "l.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_merges_related_threads(self):
        add_thread(self.db, 1, "直播和C8男面基", 1700000000, users=(2, 3))
        add_thread(self.db, 2, "吧里C8男很火", 1700003600, users=(2, 5),
                   body="C8 男 太典了")
        add_thread(self.db, 3, "周末去哪玩", 1700007200, forum="旅游",
                   users=(11, 12), body="爬山 露营 攻略")
        stories = linker.build_stories(self.db, threshold=0.5)
        self.assertEqual(len(stories), 1, stories)
        story = self.db.get_story(stories[0]["id"])
        tids = [t["tid"] for t in story["threads"]]
        self.assertEqual(tids, [1, 2])
        self.assertEqual(story["threads"][0]["role"], "主贴")

    def test_timeline_is_merged_and_sorted(self):
        add_thread(self.db, 1, "直播和C8男面基", 1700000000, users=(2, 3))
        add_thread(self.db, 2, "吧里C8男很火", 1700003600, users=(2, 5),
                   body="C8 男 太典了")
        story = linker.build_stories(self.db, threshold=0.5)[0]
        rows = linker.story_timeline(self.db, story["id"])
        self.assertTrue(any(r["label"] == "T1" for r in rows))
        self.assertTrue(any(r["label"] == "T2" for r in rows))
        times = [r["ts"] for r in rows]
        self.assertEqual(times, sorted(times))

    def test_unrelated_not_merged(self):
        add_thread(self.db, 1, "直播和C8男面基", 1700000000, users=(2, 3))
        add_thread(self.db, 2, "周末去哪玩", 1700003600, forum="旅游",
                   users=(11, 12), body="爬山 露营 攻略")
        stories = linker.build_stories(self.db, threshold=0.5)
        self.assertEqual(stories, [])


if __name__ == "__main__":
    unittest.main()
