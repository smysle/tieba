#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gossip import tieba, util  # noqa: E402


class SignTest(unittest.TestCase):
    def test_sign_matches_formula(self):
        params = {"a": "1", "b": "2", "_client_type": "20"}
        expected = hashlib.md5(
            ("_client_type=20" + "a=1" + "b=2" + tieba.PC_SALT).encode()).hexdigest()
        self.assertEqual(tieba.sign_params(params), expected)

    def test_sign_is_order_independent(self):
        one = tieba.sign_params({"kw": "nba", "pn": "1"})
        two = tieba.sign_params({"pn": "1", "kw": "nba"})
        self.assertEqual(one, two)


class ParseHelpersTest(unittest.TestCase):
    def test_parse_tid(self):
        cases = {
            "11154226751": 11154226751,
            "https://tieba.baidu.com/p/11154226751": 11154226751,
            "https://tieba.baidu.com/p/11154226751?pn=2": 11154226751,
            "tid=11154226751": 11154226751,
            "": None,
            "nba": None,
        }
        for raw, expected in cases.items():
            self.assertEqual(util.parse_tid(raw), expected, raw)

    def test_content_text(self):
        content = [{"type": 0, "text": "hello "}, {"type": 3}, {"type": 2, "c": "[笑]"}]
        self.assertEqual(tieba._content_text(content), "hello [图片][笑]")

    def test_fmt_time_empty(self):
        self.assertEqual(tieba.fmt_time(0), "")


class StableHashTest(unittest.TestCase):
    def test_stable(self):
        self.assertEqual(util.stable_hash("abc", 4), util.stable_hash("abc", 4))
        self.assertLess(util.stable_hash("abc", 4), 4)


if __name__ == "__main__":
    unittest.main()
