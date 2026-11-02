#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""贴吧抽奖：回复去重 → 抽奖 → 可复核凭证。

用法速览
  python3 lottery.py stats  --input data/replies.jsonl
  python3 lottery.py draw   --input data/replies.jsonl --prize "KFC=150" --backup 50
  python3 lottery.py verify --proof results/xxx/proof.json --input data/replies.jsonl
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import tieba_parse as tp

ALGORITHM = "splitmix64+fisher-yates/v1"
MASK64 = (1 << 64) - 1


class SplitMix64:
    """自实现 PRNG，保证跨平台、跨 Python 版本结果一致，抽奖可复现。"""

    __slots__ = ("state",)

    def __init__(self, seed_hex: str) -> None:
        seed = (seed_hex or "").strip()
        if not seed:
            raise ValueError("seed 不能为空")
        if re.fullmatch(r"[0-9a-fA-F]{1,64}", seed):
            self.state = int(seed, 16) & MASK64
        else:
            digest = hashlib.sha256(seed.encode("utf-8")).digest()
            self.state = int.from_bytes(digest[:8], "big") & MASK64

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return (z ^ (z >> 31)) & MASK64

    def randbelow(self, n: int) -> int:
        if n <= 0:
            raise ValueError("randbelow(n<=0)")
        bits = n.bit_length()
        while True:
            r = self.next_u64() >> (64 - bits)
            if r < n:
                return r


def shuffle(seq: List, rng: SplitMix64) -> None:
    for i in range(len(seq) - 1, 0, -1):
        j = rng.randbelow(i + 1)
        seq[i], seq[j] = seq[j], seq[i]


def new_seed() -> str:
    return secrets.token_hex(16)


def dedupe_key(rec: dict) -> Tuple[str, str]:
    uid = rec.get("user_id") or 0
    if uid:
        return "uid:%d" % uid, "user_id"
    name = (rec.get("user_name") or "").strip()
    if name:
        return "un:%s" % name.lower(), "user_name"
    portrait = (rec.get("portrait") or "").strip()
    if portrait:
        return "pt:%s" % portrait, "portrait"
    nick = (rec.get("nickname") or "").strip()
    if nick:
        return "nk:%s" % nick, "nickname"
    return "anon:%s" % (rec.get("floor") or rec.get("post_id")), "anonymous"


def parse_floor_range(spec: Optional[str]) -> Optional[Tuple[int, int]]:
    if not spec:
        return None
    m = re.match(r"^\s*(\d+)\s*[-~,]\s*(\d+)\s*$", str(spec))
    if not m:
        raise SystemExit("--floor-range 格式应为 A-B，例如 2-1010")
    lo, hi = int(m.group(1)), int(m.group(2))
    return (min(lo, hi), max(lo, hi))


def load_blacklist(spec: Optional[str]) -> List[str]:
    if not spec:
        return []
    path = Path(spec)
    if not path.exists():
        return [s.strip() for s in str(spec).split(",") if s.strip()]
    items: List[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(line)
    return items


def _filter_replies(replies: Sequence[dict], opts: dict) -> Tuple[List[dict], dict]:
    stats = {
        "total_replies": len(replies),
        "excluded_op": 0,
        "out_of_range": 0,
        "keyword_miss": 0,
        "too_short": 0,
        "blacklisted": 0,
    }
    floor_range = opts.get("floor_range")
    keyword = (opts.get("require_keyword") or "").strip().lower()
    min_length = int(opts.get("min_length") or 0)
    blacklist = {b.strip().lower() for b in opts.get("blacklist") or []}
    exclude_op = bool(opts.get("exclude_op"))

    filtered: List[dict] = []
    for rec in replies:
        if exclude_op and rec.get("_is_op"):
            stats["excluded_op"] += 1
            continue
        floor = rec.get("floor") or 0
        if floor_range and not (floor_range[0] <= floor <= floor_range[1]):
            stats["out_of_range"] += 1
            continue
        content = rec.get("content") or ""
        if keyword and keyword not in content.lower():
            stats["keyword_miss"] += 1
            continue
        if min_length and len(content.strip()) < min_length:
            stats["too_short"] += 1
            continue
        if blacklist:
            ident = {
                str(rec.get("user_name") or "").lower(),
                str(rec.get("nickname") or "").lower(),
                str(rec.get("user_id") or ""),
            }
            if ident & blacklist:
                stats["blacklisted"] += 1
                continue
        filtered.append(rec)
    return filtered, stats


def build_pool(replies: Sequence[dict], opts: dict) -> Tuple[List[dict], dict]:
    filtered, stats = _filter_replies(replies, opts)
    stats["kept_replies"] = len(filtered)
    stats["duplicate_hits"] = 0
    stats["keymix"] = {}

    pool: Dict[str, dict] = {}
    for rec in filtered:
        key, kind = dedupe_key(rec)
        stats["keymix"][kind] = stats["keymix"].get(kind, 0) + 1
        if key in pool:
            stats["duplicate_hits"] += 1
            pool[key]["replies"] += 1
            if (rec.get("floor") or 10**9) < pool[key]["floor"]:
                pool[key]["floor"] = rec.get("floor") or pool[key]["floor"]
        else:
            pool[key] = {
                "key": key,
                "floor": rec.get("floor") or 0,
                "user_id": rec.get("user_id") or 0,
                "user_name": (rec.get("user_name") or "").strip(),
                "nickname": (rec.get("nickname") or rec.get("user_name") or "").strip(),
                "replies": 1,
                "first_date": rec.get("date") or "",
                "key_kind": kind,
            }

    participants = sorted(pool.values(), key=lambda p: p["key"])
    stats["unique_users"] = len(participants)
    stats["anonymous"] = sum(1 for p in participants if p["key_kind"] == "anonymous")
    return participants, stats


def parse_prizes(specs: Optional[Sequence[str]]) -> List[dict]:
    specs = list(specs or [])
    if not specs:
        specs = ["奖品=150"]
    prizes = []
    for spec in specs:
        m = re.match(r"^\s*(?P<name>.+?)\s*[=:：]\s*(?P<num>\d+)\s*$", spec)
        if not m:
            raise SystemExit('--prize 格式应为 "名称=数量"，例如 --prize "KFC=150"')
        prizes.append({"name": m.group("name").strip(), "count": int(m.group("num"))})
    return prizes


def make_order(participants: Sequence[dict], seed: str, weight: bool) -> List[str]:
    rng = SplitMix64(seed)
    if weight:
        idx_pool: List[int] = []
        for i, p in enumerate(participants):
            idx_pool.extend([i] * max(1, int(p.get("replies") or 1)))
        shuffle(idx_pool, rng)
        order: List[str] = []
        seen = set()
        for i in idx_pool:
            if i in seen:
                continue
            seen.add(i)
            order.append(participants[i]["key"])
        for i, p in enumerate(participants):
            if i not in seen:
                order.append(p["key"])
        return order
    order = [p["key"] for p in participants]
    shuffle(order, rng)
    return order


def run_draw(participants: Sequence[dict], prizes: Sequence[dict], seed: str,
             backups: int, allow_repeat: bool, weight: bool) -> dict:
    by_key = {p["key"]: p for p in participants}
    winners: List[dict] = []
    shortfall = 0

    if allow_repeat:
        for prize in prizes:
            order = make_order(participants, seed + ":" + str(prize["name"]), weight)
            for n in range(1, int(prize["count"]) + 1):
                if n > len(order):
                    shortfall += 1
                    continue
                key = order[n - 1]
                winners.append(_winner_row(prize["name"], n, by_key[key]))
        order = make_order(participants, seed + ":backup", weight)
    else:
        order = make_order(participants, seed, weight)
        cursor = 0
        for prize in prizes:
            for n in range(1, int(prize["count"]) + 1):
                if cursor >= len(order):
                    shortfall += 1
                    continue
                key = order[cursor]
                cursor += 1
                winners.append(_winner_row(prize["name"], n, by_key[key]))

    won = {w["key"] for w in winners}
    backup_keys = [k for k in order if k not in won][: max(0, int(backups))]
    backup = [_backup_row(i + 1, by_key[k]) for i, k in enumerate(backup_keys)]

    return {
        "seed": seed,
        "order": order,
        "winners": winners,
        "backup": backup,
        "shortfall": shortfall,
    }


def _winner_row(prize: str, seq: int, p: dict) -> dict:
    return {
        "prize": prize,
        "seq": seq,
        "key": p["key"],
        "floor": p["floor"],
        "user_name": p["user_name"],
        "nickname": p["nickname"],
        "user_id": p["user_id"],
        "replies": p["replies"],
    }


def _backup_row(seq: int, p: dict) -> dict:
    return {
        "seq": seq,
        "key": p["key"],
        "floor": p["floor"],
        "user_name": p["user_name"],
        "nickname": p["nickname"],
        "user_id": p["user_id"],
    }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def input_fingerprint(replies: Sequence[dict]) -> str:
    h = hashlib.sha256()
    for rec in sorted(replies, key=lambda r: (r.get("floor") or 0, r.get("post_id") or 0)):
        h.update(json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def pool_fingerprint(participants: Sequence[dict]) -> str:
    payload = "\n".join("%s|%s" % (p["key"], p["floor"])
                        for p in sorted(participants, key=lambda x: x["key"]))
    return sha256_text(payload)


def result_fingerprint(winners: Sequence[dict]) -> str:
    payload = "\n".join("%s|%s|%s" % (w["prize"], w["seq"], w["key"]) for w in winners)
    return sha256_text(payload)


def _write_csv(path: Path, headers: list, rows: list) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        w.writerows(rows)


def write_winners_csv(path: Path, winners: Sequence[dict]) -> None:
    rows = [[w["seq"], w["prize"], w["floor"], w["user_name"], w["nickname"], w["user_id"], w["replies"]]
            for w in winners]
    _write_csv(path, ["序号", "奖品", "楼层", "用户名", "昵称", "用户ID", "回复次数"], rows)


def write_backup_csv(path: Path, backup: Sequence[dict]) -> None:
    rows = [[b["seq"], b["floor"], b["user_name"], b["nickname"], b["user_id"]] for b in backup]
    _write_csv(path, ["候补序号", "楼层", "用户名", "昵称", "用户ID"], rows)


def write_participants_csv(path: Path, participants: Sequence[dict]) -> None:
    rows = [[i, p["floor"], p["user_name"], p["nickname"], p["user_id"], p["replies"], p["key_kind"]]
            for i, p in enumerate(participants, start=1)]
    _write_csv(path, ["序号", "楼层", "用户名", "昵称", "用户ID", "回复次数", "去重键类型"], rows)


def write_winners_md(path: Path, winners: Sequence[dict], proof: dict) -> None:
    at_list = " ".join("@" + (w["user_name"] or w["nickname"]) for w in winners if (w["user_name"] or w["nickname"]))
    lines = [
        "# 抽奖结果 · %s" % proof["created_at"],
        "",
        "- 奖品：%s" % "、".join("%s × %d" % (p["name"], p["count"]) for p in proof["prizes"]),
        "- 总回复：%d 条 → 去重后 %d 人参与" % (proof["stats"]["total_replies"], proof["stats"]["unique_users"]),
        "- 随机种子：`%s`" % proof["seed"],
        "- 校验指纹：`%s`" % proof["result_sha256"],
        "",
        "## @ 名单（可直接粘贴）",
        "",
        at_list or "（无）",
        "",
        "## 中奖明细",
        "",
        "| 序号 | 奖品 | 楼层 | 用户名 | 昵称 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for w in winners:
        lines.append("| %d | %s | %d | %s | %s |" % (w["seq"], w["prize"], w["floor"], w["user_name"], w["nickname"]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def pick_out_dir(base: Path, prizes: Sequence[dict]) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    slug = "_".join("%s%d" % (re.sub(r"[^\w\-]+", "", p["name"]) or "prize", p["count"]) for p in prizes)
    target = base / ("%s_%s" % (stamp, slug))
    n = 2
    while target.exists():
        target = base / ("%s_%s-%d" % (stamp, slug, n))
        n += 1
    return target


def cmd_collect(args) -> int:
    tid = args.tid
    records: List[dict] = []

    if args.html:
        files = []
        for path in args.html:
            pth = Path(path)
            if not pth.exists():
                raise SystemExit("找不到文件：%s" % path)
            if pth.is_dir():
                files.extend(sorted(pth.glob("*.htm")) + sorted(pth.glob("*.html")))
            else:
                files.append(pth)
        if not files:
            raise SystemExit("目录里没有 .html 文件：%s" % ", ".join(args.html))
        for path in files:
            got = tp.parse_html_file(str(path), tid=tid)
            records.extend(got)
            print("解析 %s → %d 条" % (path.name, len(got)))
    elif args.clipboard:
        try:
            text = subprocess.check_output(["pbpaste"]).decode("utf-8", "replace")
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
            raise SystemExit("读剪贴板失败：%s" % exc)
        if not text.strip():
            raise SystemExit("剪贴板是空的")
        records.extend(tp.parse_text_text(text, src="clipboard", tid=tid))
        print("解析剪贴板 → %d 条" % len(records))
    elif args.text:
        records.extend(tp.parse_text_text(tp.read_text(args.text), src="text:%s" % Path(args.text).name, tid=tid))
        print("解析 %s → %d 条" % (args.text, len(records)))
    elif args.csv:
        records.extend(tp.parse_csv_file(args.csv, tid=tid))
        print("解析 %s → %d 条" % (args.csv, len(records)))
    elif tid:
        cookie = load_cookie(args.cookie_file) if args.cookie_file else ""
        if not cookie:
            print("（未带 cookie：走 r=1 倒序匿名也能翻完；结果偏少时再补 --cookie-file）")
        print("走网页端接口 /c/f/pb/page_pc 抓取 tid=%s …" % tid)
        records.extend(tp.fetch_pc_api(tid, cookie=cookie, pages=args.pages,
                                       delay=args.delay, log=print))
        print("抓取完成 → %d 条" % len(records))
    else:
        raise SystemExit("collect 需要 --html / --text / --csv / --clipboard / --tid 之一")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    merged = tp.dedupe_records((tp.read_jsonl(str(out)) if args.append and out.exists() else []) + records)
    count = tp.write_jsonl(merged, str(out))
    print("写入 %s → %d 条（去重后）" % (out, count))
    return 0


def load_cookie(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return ""
    if "=" not in text:
        return "BDUSS=" + text
    return " ".join(text.split())


def _opts_from_args(args) -> dict:
    floor_range = parse_floor_range(args.floor_range)
    return {
        "exclude_op": bool(args.exclude_op),
        "floor_range": tuple(floor_range) if floor_range else None,
        "require_keyword": args.require_keyword or "",
        "min_length": int(args.min_length or 0),
        "blacklist": load_blacklist(args.blacklist),
        "weight_by_replies": bool(args.weight_by_replies),
    }


def _load_and_pool(args):
    path = Path(args.input)
    if not path.exists():
        raise SystemExit("找不到输入：%s（先跑 collect）" % path)
    replies = tp.read_jsonl(str(path))
    if not replies:
        raise SystemExit("输入为空：%s" % path)
    opts = _opts_from_args(args)
    participants, stats = build_pool(replies, opts)
    return replies, opts, participants, stats


def cmd_stats(args) -> int:
    replies, opts, participants, stats = _load_and_pool(args)
    print("=" * 56)
    print("输入文件        %s" % args.input)
    print("抓到的回复      %d 条" % stats["total_replies"])
    if stats["excluded_op"]:
        print("剔除楼主        %d 条" % stats["excluded_op"])
    if stats["out_of_range"]:
        print("楼层区间外      %d 条" % stats["out_of_range"])
    if stats["keyword_miss"]:
        print("不含关键词      %d 条" % stats["keyword_miss"])
    if stats["too_short"]:
        print("过短            %d 条" % stats["too_short"])
    if stats["blacklisted"]:
        print("黑名单          %d 条" % stats["blacklisted"])
    print("-" * 56)
    print("有效回复        %d 条" % stats["kept_replies"])
    print("重复回复        %d 条（同一人多回）" % stats["duplicate_hits"])
    print("去重后参与人    %d 人" % stats["unique_users"])
    if stats["anonymous"]:
        print("其中匿名/无ID   %d 人" % stats["anonymous"])
    print("去重依据        %s" % ", ".join("%s×%d" % (k, v) for k, v in sorted(stats["keymix"].items())))
    if participants:
        multi = sum(1 for p in participants if p["replies"] > 1)
        top = sorted(participants, key=lambda p: -p["replies"])[:3]
        print("多次回复的人    %d 人" % multi)
        print("最活跃          %s" % "；".join("%s(%d次)" % (p["user_name"] or p["nickname"], p["replies"]) for p in top))
    print("=" * 56)
    return 0


def cmd_draw(args) -> int:
    replies, opts, participants, stats = _load_and_pool(args)
    prizes = parse_prizes(args.prize)
    need = sum(p["count"] for p in prizes)
    seed = (args.seed or "").strip() or new_seed()
    seed = seed.lower()

    print("去重后 %d 人参与，奖品共 %d 份" % (len(participants), need))
    if not participants:
        raise SystemExit("没有有效参与人，先检查过滤条件")

    if args.dry_run:
        sample = run_draw(participants, [{"name": "预览", "count": min(need, len(participants))}],
                          seed, args.backup, args.allow_repeat, opts["weight_by_replies"])
        print("试运行 seed=%s" % seed)
        for w in sample["winners"][:10]:
            print("  %2d. %d楼 %s" % (w["seq"], w["floor"], w["user_name"] or w["nickname"]))
        if len(sample["winners"]) > 10:
            print("  … 共 %d 名" % len(sample["winners"]))
        if sample["shortfall"]:
            print("警告：参与人数不足，缺 %d 份" % sample["shortfall"])
        return 0

    result = run_draw(participants, prizes, seed, args.backup, args.allow_repeat,
                      opts["weight_by_replies"])
    if result["shortfall"]:
        print("警告：参与人数 %d < 奖品 %d，缺 %d 份未抽出（已记录 shortfall）"
              % (len(participants), need, result["shortfall"]))

    out_dir = pick_out_dir(Path(args.out), prizes)
    out_dir.mkdir(parents=True, exist_ok=True)

    proof = {
        "version": 1,
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "tid": int(args.tid or 0),
        "source": str(args.input),
        "prizes": prizes,
        "seed": seed,
        "algorithm": ALGORITHM,
        "allow_repeat": bool(args.allow_repeat),
        "backup_count": int(args.backup),
        "shortfall": result["shortfall"],
        "options": {
            "exclude_op": opts["exclude_op"],
            "floor_range": list(opts["floor_range"]) if opts["floor_range"] else None,
            "require_keyword": opts["require_keyword"],
            "min_length": opts["min_length"],
            "blacklist": opts["blacklist"],
            "weight_by_replies": opts["weight_by_replies"],
        },
        "stats": stats,
        "input_sha256": input_fingerprint(replies),
        "pool_sha256": pool_fingerprint(participants),
        "result_sha256": result_fingerprint(result["winners"]),
        "python": sys.version.split()[0],
        "winners": result["winners"],
        "backup": result["backup"],
    }

    write_winners_csv(out_dir / "winners.csv", result["winners"])
    write_backup_csv(out_dir / "backup.csv", result["backup"])
    write_participants_csv(out_dir / "participants.csv", participants)
    write_winners_md(out_dir / "winners.md", result["winners"], proof)
    (out_dir / "proof.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")
    try:
        import report as rp
        rp.render(proof, out_dir / "report.html")
    except (ImportError, IOError, ValueError) as exc:
        print("（报告渲染跳过：%s）" % exc)

    print("-" * 56)
    for w in result["winners"][:10]:
        print("  %3d. %-6s %5d楼  %s" % (w["seq"], w["prize"], w["floor"], w["user_name"] or w["nickname"]))
    if len(result["winners"]) > 10:
        print("  … 共 %d 名中奖（完整名单见目录）" % len(result["winners"]))
    print("-" * 56)
    print("seed        %s" % seed)
    print("指纹        %s" % proof["result_sha256"][:32])
    print("输出目录    %s" % out_dir)
    return 0


def cmd_verify(args) -> int:
    proof_path = Path(args.proof)
    if not proof_path.exists():
        raise SystemExit("找不到 %s" % proof_path)
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    src = Path(args.input or proof.get("source") or "")
    if not src.exists():
        raise SystemExit("找不到原始数据：%s（用 --input 指定）" % src)

    replies = tp.read_jsonl(str(src))
    opts = dict(proof.get("options") or {})
    if opts.get("floor_range"):
        opts["floor_range"] = tuple(opts["floor_range"])
    participants, stats = build_pool(replies, opts)

    ok = True
    checks = []

    fp_in = input_fingerprint(replies)
    checks.append(("输入指纹", fp_in == proof["input_sha256"], "%s…" % fp_in[:16]))
    fp_pool = pool_fingerprint(participants)
    checks.append(("名单指纹", fp_pool == proof["pool_sha256"], "%d 人" % len(participants)))
    replay = run_draw(participants, proof["prizes"], proof["seed"], proof.get("backup_count", 0),
                      proof.get("allow_repeat", False), bool(opts.get("weight_by_replies")))
    fp_res = result_fingerprint(replay["winners"])
    checks.append(("结果指纹", fp_res == proof["result_sha256"], "%s…" % fp_res[:16]))
    same_names = [w["key"] for w in replay["winners"]] == [w["key"] for w in proof.get("winners") or []]
    checks.append(("中奖名单", same_names, "%d 名" % len(replay["winners"])))

    print("=" * 56)
    for name, passed, detail in checks:
        print("%s %-8s %s" % ("✓" if passed else "✗", name, detail))
        ok = ok and passed
    print("-" * 56)
    if ok:
        print("复核通过：数据、seed、结果三者完全一致。")
    else:
        print("复核失败：输入或环境与公证记录不一致（帖子可能被改过/删楼）。")
    print("=" * 56)
    return 0 if ok else 2


def add_filter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--exclude-op", action="store_true", help="剔除楼主楼层")
    p.add_argument("--floor-range", default=None, help="只统计楼层区间，如 2-1010")
    p.add_argument("--require-keyword", default=None, help="回复必须包含的关键词，如 KFC")
    p.add_argument("--min-length", type=int, default=0, help="回复最短字数")
    p.add_argument("--blacklist", default=None, help="黑名单文件（一行一个用户名/ID）")
    p.add_argument("--weight-by-replies", action="store_true", help="按回复次数加权（默认一人一票）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lottery.py", description="贴吧抽奖：去重 → 抽奖 → 可复核凭证")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="采集回复到 data/replies.jsonl")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--html", nargs="+", help="已保存的帖子网页（可多个，也可直接给目录）")
    g.add_argument("--text", help="纯文本回复列表")
    g.add_argument("--csv", help="CSV，表头含 楼层/用户名/内容")
    g.add_argument("--clipboard", action="store_true", help="直接从剪贴板读取（macOS pbpaste）")
    c.add_argument("--tid", type=int, default=0, help="帖子 tid（也可用于 cookie 抓取）")
    c.add_argument("--cookie-file", default=None, help="Cookie 文件（BDUSS=... 或裸 BDUSS）")
    c.add_argument("--pages", default=None, help="抓取页范围，如 1-34")
    c.add_argument("--delay", type=float, default=1.2, help="翻页间隔秒")
    c.add_argument("--out", default="data/replies.jsonl")
    c.add_argument("--append", action="store_true", help="追加合并到已有 jsonl")
    c.set_defaults(func=cmd_collect)

    s = sub.add_parser("stats", help="只看去重结果，不抽奖")
    s.add_argument("--input", default="data/replies.jsonl")
    add_filter_args(s)
    s.set_defaults(func=cmd_stats)

    d = sub.add_parser("draw", help="抽奖并生成名单/凭证/报告")
    d.add_argument("--input", default="data/replies.jsonl")
    d.add_argument("--prize", action="append", help='奖品，可多次：--prize "KFC=150"')
    d.add_argument("--backup", type=int, default=20, help="候补人数")
    d.add_argument("--seed", default=None, help="指定随机种子（复现用）")
    d.add_argument("--allow-repeat", action="store_true", help="允许同一人跨奖品重复中奖")
    d.add_argument("--dry-run", action="store_true", help="只预演，不写文件")
    d.add_argument("--out", default="results")
    d.add_argument("--tid", type=int, default=0)
    add_filter_args(d)
    d.set_defaults(func=cmd_draw)

    v = sub.add_parser("verify", help="用 proof.json 复核结果")
    v.add_argument("--proof", required=True)
    v.add_argument("--input", default=None)
    v.set_defaults(func=cmd_verify)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args) or 0)
    except tp.ParseError as exc:
        print("采集失败：%s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
