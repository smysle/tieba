#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gossip import vision  # noqa: E402


class ExtractJsonTest(unittest.TestCase):
    def test_picks_last_valid_json_from_noise(self):
        text = ('{"delta":{"text":"我会"}}\n'
                '```json\n{"type":"chat","ocr_text":"14:22 好了么","confidence":0.9}\n```')
        out = vision._extract_json(text)
        self.assertEqual(out["type"], "chat")
        self.assertEqual(out["confidence"], 0.9)

    def test_falls_back_to_text(self):
        out = vision._extract_json("完全不是 JSON")
        self.assertEqual(out["type"], "other")
        self.assertIn("完全不是", out["ocr_text"])

    def test_guess_type(self):
        self.assertEqual(vision._guess_type("14:22 好了么 按住说话"), "chat")
        self.assertEqual(vision._guess_type("转账 500 元"), "transaction")
        self.assertEqual(vision._guess_type("", 100, 400), "document")


if __name__ == "__main__":
    unittest.main()
