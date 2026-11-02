#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片下载、缓存、缩略图。

贴吧图片 URL 带 `tbpicau` 时效 token，抓到就下载；按 sha1 去重，原图 + 缩略图各存一份。
"""
from __future__ import annotations

import hashlib
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional

from . import util

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_BYTES = 20 * 1024 * 1024
THUMB_WIDTH = 520


class MediaError(RuntimeError):
    pass


def _ext_from_mime(mime: str, fallback: str = ".jpg") -> str:
    mime = (mime or "").lower()
    if "png" in mime:
        return ".png"
    if "webp" in mime:
        return ".webp"
    if "gif" in mime:
        return ".gif"
    return fallback


class MediaStore:
    def __init__(self, db, root: str) -> None:
        self.db = db
        self.root = os.path.abspath(root)
        util.ensure_dir(os.path.join(self.root, "thumb"))

    # ------------------------------------------------------------ 元数据登记
    def register_thread(self, tid: int) -> int:
        """把 floors.media_json 展开进 media 表（只登记，不下载）。"""
        rows = []
        for floor in self.db.floors_with_media(tid):
            items = util.json_loads(floor.get("media_json"), []) or []
            for item in items:
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                rows.append({
                    "tid": tid,
                    "floor": floor.get("floor") or 0,
                    "sub_id": floor.get("sub_id") or 0,
                    "pid": floor.get("pid") or 0,
                    "pic_id": item.get("pic_id") or "",
                    "url": item.get("url") or "",
                    "origin_url": item.get("origin_url") or "",
                    "width": item.get("width") or 0,
                    "height": item.get("height") or 0,
                })
        return self.db.add_media(rows) if rows else 0

    # ---------------------------------------------------------------- 下载
    def _fetch(self, url: str, referer: str = "https://tieba.baidu.com/") -> bytes:
        headers = {
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read(MAX_BYTES + 1)
                mime = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise MediaError("图片 HTTP %d" % exc.code)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MediaError("图片下载失败：%s" % exc)
        if len(data) > MAX_BYTES:
            raise MediaError("图片超过 %d MB" % (MAX_BYTES // 1024 // 1024))
        if not data:
            raise MediaError("图片为空")
        return data, mime  # type: ignore[return-value]

    def download(self, row: dict, force: bool = False) -> dict:
        media_id = int(row["id"])
        if row.get("status") == "done" and row.get("path") and not force:
            return row
        urls = [u for u in (row.get("origin_url"), row.get("url")) if u]
        if not urls:
            raise MediaError("没有图片地址")
        last_error = ""
        data = b""
        mime = ""
        for url in urls:
            try:
                data, mime = self._fetch(url, referer="https://tieba.baidu.com/p/%s" % row["tid"])
                break
            except MediaError as exc:
                last_error = str(exc)
        if not data:
            self.db.update_media(media_id, status="error", error=last_error[:300])
            raise MediaError(last_error or "下载失败")

        sha1 = hashlib.sha1(data).hexdigest()
        ext = _ext_from_mime(mime)
        folder = os.path.join(self.root, sha1[:2])
        util.ensure_dir(folder)
        path = os.path.join(folder, sha1 + ext)
        if not os.path.exists(path):
            util.atomic_write(path, data)

        thumb_path = os.path.join(self.root, "thumb", sha1 + ".jpg")
        width = height = 0
        try:
            from PIL import Image
            with Image.open(path) as img:
                width, height = img.size
                if not os.path.exists(thumb_path):
                    img = img.convert("RGB")
                    if img.width > THUMB_WIDTH:
                        ratio = THUMB_WIDTH / float(img.width)
                        img = img.resize((THUMB_WIDTH, max(1, int(img.height * ratio))))
                    img.save(thumb_path, "JPEG", quality=82)
        except Exception:
            thumb_path = ""  # 没有 PIL 或图片损坏，缩略图留空

        self.db.update_media(
            media_id, path=path, thumb=thumb_path, sha1=sha1, mime=mime,
            width=width, height=height, bytes=len(data), status="downloaded", error="",
        )
        row = dict(row)
        row.update({"path": path, "thumb": thumb_path, "sha1": sha1,
                    "status": "downloaded", "width": width, "height": height})
        return row

    def download_pending(self, tid: int, limit: int = 40,
                         log: Optional[Callable[[str], None]] = None) -> dict:
        rows = self.db.media_rows(tid, status="new", limit=limit)
        if not rows:
            rows = [r for r in self.db.media_rows(tid, status="downloaded", limit=500)
                    if not r.get("thumb")][:limit]
        done = failed = 0
        for row in rows:
            try:
                self.download(row)
                done += 1
                if log:
                    log("图片 %d/%d 下载完成" % (done, len(rows)))
            except MediaError as exc:
                failed += 1
                if log:
                    log("图片失败：%s" % exc)
            time.sleep(0.15)
        return {"downloaded": done, "failed": failed, "total": len(rows)}
