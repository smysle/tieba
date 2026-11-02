#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


FLOORS = [
    {"floor": 1, "sub_id": 0, "user_id": 1, "user": "蛋芽",
     "content": "直播面基，他说自己是投行的。", "ts": 1793500000, "is_op": True},
    {"floor": 2, "sub_id": 0, "user_id": 2, "user": "路人A",
     "content": "笑死，投行男不会这么说话", "ts": 1793500300, "likes": 2},
    {"floor": 3, "sub_id": 0, "user_id": 3, "user": "对线B",
     "content": "凭什么说人家假？你自己不也背高仿", "ts": 1793500600},
    {"floor": 4, "sub_id": 0, "user_id": 1, "user": "蛋芽",
     "content": "我报警了，做了笔录。", "ts": 1793501000, "is_op": True},
]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = app_module.App(os.path.join(cls.tmp.name, "api.db"), enable_monitor=False)
        cls.app.db.upsert_thread({"tid": 7, "title": "测试面基贴", "author": "蛋芽",
                                  "author_id": 1, "forum": "憨批", "reply_num": 4})
        cls.app.db.upsert_floors(7, FLOORS)
        cls.app.db.refresh_thread_counts(7)
        app_module.Handler.app = cls.app
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.shutdown()
        cls.tmp.cleanup()

    def call(self, path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_index(self):
        status, body = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn("瓜田雷达", body)

    def test_state(self):
        status, body = self.call("/api/state")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["threads"]), 1)
        self.assertEqual(data["threads"][0]["title"], "测试面基贴")
        self.assertFalse(data["monitor"]["running"])

    def test_thread_detail(self):
        status, body = self.call("/api/threads/7?mode=op")
        data = json.loads(body)
        self.assertEqual(len(data["floors"]), 2)
        self.assertEqual(data["stats"]["floors"], 4)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/api/threads/999")
        self.assertEqual(ctx.exception.code, 404)

    def test_star(self):
        status, body = self.call("/api/floors/7/2/star", "POST", {"starred": True})
        self.assertTrue(json.loads(body)["ok"])
        status, body = self.call("/api/threads/7?mode=starred")
        self.assertEqual(len(json.loads(body)["floors"]), 1)

    def test_summarize_job_and_report(self):
        status, body = self.call("/api/summarize", "POST",
                                 {"tid": 7, "mode": "daily", "provider": "local"})
        job_id = json.loads(body)["job_id"]
        job = None
        for _ in range(60):
            _, body = self.call("/api/jobs/" + job_id)
            job = json.loads(body)["job"]
            if job["status"] != "running":
                break
            time.sleep(0.15)
        self.assertEqual(job["status"], "done", job)
        report_id = job["result"]["report_id"]

        _, body = self.call("/api/reports/" + report_id + "/html")
        self.assertIn("吃瓜", body)
        self.assertIn("保存长图", body)

        _, body = self.call("/api/reports/" + report_id + "/md")
        self.assertIn("## 时间轴", body)

    def test_media_and_stories_empty(self):
        status, body = self.call("/api/threads/7/media")
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(json.loads(body)["media"], [])
        status, body = self.call("/api/stories")
        self.assertTrue(json.loads(body)["ok"])
        status, body = self.call("/api/stories/build", "POST", {})
        self.assertEqual(json.loads(body)["stories"], [])

    def test_sources_crud(self):
        status, body = self.call("/api/sources", "POST",
                                 {"kind": "thread", "key": "11154226751", "interval": 60})
        source = json.loads(body)["source"]
        self.assertEqual(source["key"], "11154226751")
        status, body = self.call("/api/sources/%d/toggle" % source["id"], "POST", {})
        self.assertFalse(json.loads(body)["enabled"])
        status, body = self.call("/api/sources/%d" % source["id"], "DELETE")
        self.assertTrue(json.loads(body)["ok"])

    def test_config_roundtrip(self):
        status, body = self.call("/api/config", "POST",
                                 {"monitor": {"max_pages": 55,
                                              "keywords": ["反转", "实锤"]}})
        cfg = json.loads(body)["config"]
        self.assertEqual(cfg["monitor"]["max_pages"], 55)
        self.assertEqual(cfg["monitor"]["keywords"], ["反转", "实锤"])

    def test_bad_request(self):
        req = urllib.request.Request("http://127.0.0.1:%d/api/summarize" % self.port,
                                     data=b"not-json", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("should fail")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
