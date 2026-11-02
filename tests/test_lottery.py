#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lottery as lt          # noqa: E402
import report as rp           # noqa: E402
import tieba_parse as tp      # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def make_replies(n=1000, users=800):
    out = []
    for i in range(1, n + 1):
        out.append({
            "floor": i,
            "user_id": 10000 + (i % users),
            "user_name": "user%03d" % (i % users),
            "nickname": "昵称%03d" % (i % users),
            "portrait": "",
            "date": "",
            "content": "我要KFC",
            "tid": 1,
            "post_id": i,
            "src": "gen",
            "_is_op": i == 1,
        })
    return out


class ParseHtmlTest(unittest.TestCase):
    def test_basic(self):
        recs = tp.parse_html_file(str(FIX / "thread_p1.html"), tid=42)
        self.assertEqual([r["floor"] for r in recs], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(recs[0]["user_name"], "op_user")
        self.assertTrue(recs[0]["_is_op"])
        self.assertEqual(recs[1]["user_name"], "alice")
        self.assertEqual(recs[1]["nickname"], "爱丽丝")
        self.assertEqual(recs[1]["user_id"], 2002)
        self.assertEqual(recs[1]["post_id"], 90002)
        self.assertEqual(recs[1]["content"], "我要KFC")
        # d_name 里的重复 data-field 不该变成额外记录
        self.assertEqual(sum(1 for r in recs if r["user_name"] == "alice"), 2)
        self.assertEqual(recs[2]["content"], "抽我抽我\n前排")
        self.assertEqual(recs[3]["_is_op"], False)

    def test_entities(self):
        recs = tp.parse_html_file(str(FIX / "thread_p1.html"))
        carol = [r for r in recs if r["user_name"] == "carol"][0]
        self.assertEqual(carol["nickname"], "卡罗尔")
        self.assertEqual(carol["floor"], 5)

    def test_no_floor_fallback(self):
        recs = tp.parse_html_file(str(FIX / "thread_nofloor.html"))
        self.assertEqual([r["floor"] for r in recs], [1, 2, 3])
        self.assertTrue(recs[0]["_is_op"])

    def test_blocked_page(self):
        page = "<html><head><title>百度安全验证</title></head><body>请输入验证码</body></html>"
        with self.assertRaises(tp.ParseError):
            tp.parse_html_text(page)

    def test_gbk_encoding(self):
        html = "<html><body>" + (
            '<div class="l_post lz" data-field=\'{"author":{"user_id":9,"user_name":"张三"},'
            '"content":{"post_id":1,"content":"我要KFC"}}\'></div>'
            '<div class="tail-info">1楼</div>'
        ) + "</body></html>"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gbk.html"
            path.write_bytes(html.encode("gb18030"))
            recs = tp.parse_html_file(str(path))
        self.assertEqual(recs[0]["user_name"], "张三")
        self.assertEqual(recs[0]["floor"], 1)

    def test_merge_same_page_twice(self):
        a = tp.parse_html_file(str(FIX / "thread_p1.html"))
        merged = tp.dedupe_records(a + a)
        self.assertEqual(len(merged), len(a))


class ParseTextCsvTest(unittest.TestCase):
    def test_text(self):
        recs = tp.parse_text_text((FIX / "replies.txt").read_text(encoding="utf-8"))
        names = [r["user_name"] for r in recs]
        self.assertIn("alice", names)
        self.assertIn("bob", names)
        self.assertIn("carol", names)
        self.assertIn("dave", names)
        self.assertIn("eve", names)
        self.assertEqual(len(recs), 6)
        self.assertTrue(recs[0]["_is_op"])

    def test_csv(self):
        recs = tp.parse_csv_file(str(FIX / "replies.csv"))
        self.assertEqual(len(recs), 5)
        self.assertEqual(recs[1]["user_name"], "alice")
        self.assertEqual(recs[1]["nickname"], "爱丽丝")
        self.assertEqual(recs[1]["floor"], 2)


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.recs = tp.parse_html_file(str(FIX / "thread_p1.html"))

    def test_dedupe_by_uid(self):
        pool, stats = lt.build_pool(self.recs, {"exclude_op": False})
        self.assertEqual(stats["total_replies"], 7)
        self.assertEqual(stats["unique_users"], 5)   # op/alice/bob/carol/匿名
        self.assertEqual(stats["duplicate_hits"], 2)  # alice 第2次 + 匿名第2次
        alice = [p for p in pool if p["user_name"] == "alice"][0]
        self.assertEqual(alice["replies"], 2)
        self.assertEqual(alice["floor"], 2)

    def test_exclude_op(self):
        pool, stats = lt.build_pool(self.recs, {"exclude_op": True})
        self.assertEqual(stats["excluded_op"], 1)
        self.assertEqual(stats["unique_users"], 4)
        self.assertNotIn("op_user", [p["user_name"] for p in pool])

    def test_keyword(self):
        pool, stats = lt.build_pool(self.recs, {"require_keyword": "KFC"})
        self.assertEqual(stats["keyword_miss"], 5)
        self.assertEqual(len(pool), 2)  # op_user + alice 的内容含 KFC

    def test_floor_range(self):
        pool, stats = lt.build_pool(self.recs, {"floor_range": (2, 3)})
        self.assertEqual(stats["kept_replies"], 2)

    def test_blacklist(self):
        pool, stats = lt.build_pool(self.recs, {"blacklist": ["bob"]})
        self.assertEqual(stats["blacklisted"], 1)
        self.assertNotIn("bob", [p["user_name"] for p in pool])

    def test_dedupe_key_priority(self):
        self.assertEqual(lt.dedupe_key({"user_id": 7, "user_name": "x"})[0], "uid:7")
        self.assertEqual(lt.dedupe_key({"user_id": 0, "user_name": "X"})[0], "un:x")
        self.assertEqual(lt.dedupe_key({"user_name": "", "portrait": "p1"})[0], "pt:p1")
        self.assertEqual(lt.dedupe_key({"user_name": "", "nickname": "n"})[0], "nk:n")


class DrawTest(unittest.TestCase):
    def setUp(self):
        self.replies = make_replies()
        self.pool, self.stats = lt.build_pool(self.replies, {"exclude_op": True})

    def test_deterministic(self):
        a = lt.run_draw(self.pool, [{"name": "KFC", "count": 150}], "00ff", 50, False, False)
        b = lt.run_draw(self.pool, [{"name": "KFC", "count": 150}], "00ff", 50, False, False)
        self.assertEqual([w["key"] for w in a["winners"]], [w["key"] for w in b["winners"]])
        self.assertEqual(lt.result_fingerprint(a["winners"]), lt.result_fingerprint(b["winners"]))

    def test_different_seed_differs(self):
        a = lt.run_draw(self.pool, [{"name": "KFC", "count": 150}], "aa", 0, False, False)
        b = lt.run_draw(self.pool, [{"name": "KFC", "count": 150}], "bb", 0, False, False)
        self.assertNotEqual([w["key"] for w in a["winners"]], [w["key"] for w in b["winners"]])

    def test_no_duplicates_and_count(self):
        res = lt.run_draw(self.pool, [{"name": "KFC", "count": 150}], "1234", 50, False, False)
        keys = [w["key"] for w in res["winners"]]
        self.assertEqual(len(keys), 150)
        self.assertEqual(len(set(keys)), 150)
        self.assertEqual(res["shortfall"], 0)
        self.assertEqual(len(res["backup"]), 50)
        self.assertFalse(set(keys) & {b["key"] for b in res["backup"]})

    def test_multi_prize_no_repeat(self):
        prizes = [{"name": "KFC", "count": 100}, {"name": "可乐", "count": 50}]
        res = lt.run_draw(self.pool, prizes, "beef", 10, False, False)
        keys = [w["key"] for w in res["winners"]]
        self.assertEqual(len(keys), 150)
        self.assertEqual(len(set(keys)), 150)

    def test_allow_repeat(self):
        prizes = [{"name": "A", "count": 300}, {"name": "B", "count": 300}]
        res = lt.run_draw(self.pool, prizes, "cafe", 0, True, False)
        self.assertEqual(len(res["winners"]), 600)
        self.assertGreater(len({w["key"] for w in res["winners"]}), 300)

    def test_shortfall(self):
        res = lt.run_draw(self.pool, [{"name": "KFC", "count": 1000}], "d00d", 0, False, False)
        self.assertEqual(len(res["winners"]), len(self.pool))
        self.assertEqual(res["shortfall"], 1000 - len(self.pool))

    def test_weighted(self):
        res = lt.run_draw(self.pool, [{"name": "KFC", "count": 5}], "feed", 0, False, True)
        self.assertEqual(len(res["winners"]), 5)
        self.assertEqual(len({w["key"] for w in res["winners"]}), 5)

    def test_fingerprints(self):
        f1 = lt.pool_fingerprint(self.pool)
        f2 = lt.pool_fingerprint(list(reversed(self.pool)))
        self.assertEqual(f1, f2)
        self.assertEqual(len(lt.input_fingerprint(self.replies)), 64)


class RandomTest(unittest.TestCase):
    def test_randbelow_uniform(self):
        rng = lt.SplitMix64("c0ffee")
        bins = [0] * 200
        for _ in range(200000):
            bins[rng.randbelow(200)] += 1
        expected = 1000.0
        chi2 = sum((b - expected) ** 2 / expected for b in bins)
        self.assertLess(chi2, 300, "chi2=%s 分布异常" % chi2)
        self.assertTrue(all(600 < b < 1400 for b in bins))

    def test_shuffle_coverage(self):
        pool = [{"key": "k%02d" % i, "floor": i, "user_name": "u%d" % i,
                 "nickname": "", "user_id": i, "replies": 1, "key_kind": "uid"}
                for i in range(60)]
        seen = set()
        for i in range(1200):
            res = lt.run_draw(pool, [{"name": "x", "count": 1}], "seed%04d" % i, 0, False, False)
            seen.add(res["winners"][0]["key"])
        self.assertEqual(len(seen), 60)

    def test_randbelow_rejects_bad(self):
        with self.assertRaises(ValueError):
            lt.SplitMix64("01").randbelow(0)



class PcApiSignTest(unittest.TestCase):
    """逆向出来的签名算法必须和线上抓到的请求逐字节一致。"""

    def test_sign_matches_captured_request(self):
        params = {
            "pn": "1", "lz": "0", "r": "2", "mark_type": "0", "back": "0", "fr": "frs",
            "kz": "11154226751", "session_request_times": "1",
            "tbs": "3128d62bec7373df1793644775", "subapp_type": "pc", "_client_type": "20",
        }
        self.assertEqual(tp.pc_sign(params), "e4e93d5905a2b4a311d495189355c484")

    def test_sign_depends_on_every_param(self):
        base = {"pn": "1", "kz": "1", "subapp_type": "pc", "_client_type": "20"}
        a = tp.pc_sign(base)
        b = tp.pc_sign(dict(base, pn="2"))
        self.assertNotEqual(a, b)

    def test_client_salt_differs(self):
        base = {"a": "1"}
        self.assertNotEqual(tp.pc_sign(base), tp.pc_sign(base, tp.CLIENT_SALT))

    def test_content_text_shapes(self):
        self.assertEqual(tp._content_text([{"type": 0, "text": "a"}, {"type": 0, "text": "b"}]), "ab")
        self.assertEqual(tp._content_text("x<br/>y"), "x\ny")
        self.assertEqual(tp._content_text(None), "")
        # 接口真实分段：2 表情（文本在 c）、3 图片、4 @人、10 语音
        self.assertEqual(tp._content_text([{"type": 2, "text": "image_emoticon16", "c": "阴险"}]), "阴险")
        self.assertEqual(tp._content_text([{"type": 3, "cdn_src": "https://x/y.png"}]), "[图片]")
        self.assertEqual(tp._content_text([{"type": 4, "text": "@信仰", "uid": 1}]), "@信仰")
        self.assertEqual(tp._content_text([{"type": 10, "voice_md5": "abc"}]), "[语音]")
        # 未知类型 / 脏数据不能炸，也不能把表情代码漏进正文
        self.assertEqual(tp._content_text([{"type": 99}, "raw", None, {"type": 0}]), "raw")



class ReportTest(unittest.TestCase):
    def test_render(self):
        replies = make_replies(60, 40)
        pool, _ = lt.build_pool(replies, {"exclude_op": True})
        res = lt.run_draw(pool, [{"name": "KFC", "count": 5}], "ab12", 3, False, False)
        proof = {
            "created_at": "2026-11-03T02:30:00+08:00", "tid": 123, "source": "x",
            "prizes": [{"name": "KFC", "count": 5}], "seed": "ab12",
            "algorithm": lt.ALGORITHM, "stats": {"total_replies": 60, "unique_users": 39,
                                                 "duplicate_hits": 0, "keymix": {"user_id": 60}},
            "input_sha256": "a" * 64, "result_sha256": "b" * 64,
            "winners": res["winners"], "backup": res["backup"], "shortfall": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.html"
            rp.render(proof, out)
            text = out.read_text(encoding="utf-8")
        self.assertIn("中奖名单", text)
        self.assertIn(res["winners"][0]["user_name"], text)
        self.assertIn("ab12", text)


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "lottery.py")] + list(args),
                              cwd=str(ROOT), capture_output=True, text=True)

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "replies.jsonl"
            r = self.run_cli("collect", "--html", str(FIX / "thread_p1.html"),
                             "--tid", "123", "--out", str(data))
            self.assertEqual(r.returncode, 0, r.stderr)

            r = self.run_cli("stats", "--input", str(data), "--exclude-op")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("去重后参与人", r.stdout)

            r = self.run_cli("draw", "--input", str(data), "--prize", "KFC=3",
                             "--backup", "1", "--exclude-op", "--out", str(Path(tmp) / "res"),
                             "--tid", "123", "--seed", "deadbeef")
            self.assertEqual(r.returncode, 0, r.stderr)
            dirs = list((Path(tmp) / "res").iterdir())
            self.assertEqual(len(dirs), 1)
            for name in ("winners.csv", "backup.csv", "participants.csv", "proof.json",
                         "winners.md", "report.html"):
                self.assertTrue((dirs[0] / name).exists(), name)

            proof = json.loads((dirs[0] / "proof.json").read_text(encoding="utf-8"))
            self.assertEqual(len(proof["winners"]), 3)
            self.assertEqual(proof["seed"], "deadbeef")

            r = self.run_cli("verify", "--proof", str(dirs[0] / "proof.json"), "--input", str(data))
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("复核通过", r.stdout)

    def test_verify_detects_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "replies.jsonl"
            self.run_cli("collect", "--html", str(FIX / "thread_p1.html"), "--out", str(data))
            self.run_cli("draw", "--input", str(data), "--prize", "KFC=2", "--exclude-op",
                         "--out", str(Path(tmp) / "res"), "--seed", "1234")
            proof_path = next((Path(tmp) / "res").glob("*/proof.json"))

            lines = data.read_text(encoding="utf-8").splitlines()
            rec = json.loads(lines[1])
            rec["user_id"] = 999999
            lines[1] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
            data.write_text("\n".join(lines) + "\n", encoding="utf-8")

            r = self.run_cli("verify", "--proof", str(proof_path), "--input", str(data))
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("复核失败", r.stdout)


class PerfTest(unittest.TestCase):
    def test_1000_replies_fast(self):
        import time
        replies = make_replies(1000, 800)
        t0 = time.time()
        pool, stats = lt.build_pool(replies, {"exclude_op": True})
        res = lt.run_draw(pool, [{"name": "KFC", "count": 150}], "perf", 50, False, False)
        elapsed = time.time() - t0
        self.assertEqual(stats["unique_users"], 800)
        self.assertEqual(len(res["winners"]), 150)
        self.assertLess(elapsed, 0.5, "1000 条耗时 %.3fs" % elapsed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
