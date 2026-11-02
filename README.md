# 贴吧抽奖

给一个帖子链接（或 tid），自动抓回复 → **按人去重** → 抽 N 份奖品 → 输出能直接发贴公示的名单 + 可复核凭证。

- 纯 Python 3 标准库，零安装（不用 `requests`、不用浏览器、不用无头 Chrome）
- 直接打贴吧网页端接口 `/c/f/pb/page_pc`，签名算法已逆向（见 [docs/tieba-api.md](docs/tieba-api.md)）
- 一人多回复只算一次（按 `user_id` 去重，不是按昵称）
- 随机种子 + 输入/名单/结果三重 SHA-256，谁都能复算
- 产出单文件 `report.html`，双击就能看、能截图发贴

---

## 30 秒跑通

```bash
cd "贴吧抽奖"

# 1. 抓回复（只给 tid 就行）
python3 lottery.py collect --tid 11154226751 --out data/replies.jsonl

# 2. 看有多少人参与
python3 lottery.py stats --input data/replies.jsonl --exclude-op

# 3. 抽 150 份，留 20 个候补
python3 lottery.py draw --input data/replies.jsonl --prize "KFC=150" --backup 20 \
        --exclude-op --tid 11154226751

# 4. 打开公示页
open results/*/report.html
```

### 实测（`tieba.baidu.com/p/11154226751`）

```
第 28/28 页 → 累计 405 条
抓取完成 → 405 条

抓到的回复      405 条
剔除楼主        27 条
有效回复        378 条
重复回复        73 条（同一人多回）
去重后参与人    305 人
→ 抽 150 名，20 个候补，0 重复
```

---

## 采集：4 种入口

| 方式 | 命令 | 说明 |
| --- | --- | --- |
| **接口直抓（推荐）** | `collect --tid 11154226751` | 走 `page_pc`，28 页全量，不需要登录 |
| 保存网页 | `collect --html data/raw/` | 浏览器 `⌘S` 存「网页，仅 HTML」，多页丢进目录 |
| 剪贴板 | `collect --clipboard` | 页面上框选回复 `⌘C`，一条命令读完 |
| CSV | `collect --csv x.csv` | 表头含 `楼层/用户名/昵称/内容` 即可 |

需要登录态时（老帖 `r=1` 被截断、要看折叠楼）：

```bash
python3 lottery.py collect --tid 11154226751 --cookie-file data/raw/cookie.txt
```

`cookie.txt` 里放 `BDUSS=xxxx` 或裸的 BDUSS 值。

### 接口是怎么打通的

网页端每个请求都带 `sign`，算法在 `base.*.js` 里：

```
sign = md5( "".join(f"{k}={v}" for k in sorted(params)) + "36770b1f34c9bbf2e7d1a99d2b82fa9e" )
```

`tbs` 从 `/c/s/pc/sync` 现取。还有个关键点：**`r=1`（倒序）匿名能翻完全部页，
`r=2`（正序）匿名只能看第 1 页** —— 页面上那句「登录后查看全部评论内容」就是 `r=2` 的分页门槛。
细节、字段含义、覆盖率说明都在 [docs/tieba-api.md](docs/tieba-api.md)。

---

## 抽奖

```bash
python3 lottery.py draw \
  --input data/replies.jsonl \
  --prize "KFC=150" --prize "可乐=20" \
  --backup 50 \
  --tid 11154226751
```

| 参数 | 说明 |
| --- | --- |
| `--prize "名称=数量"` | 可写多次，按顺序抽 |
| `--backup 20` | 候补人数，中奖者放弃时顺延 |
| `--seed <hex或任意字符串>` | 指定种子，用来复现 / 公证 |
| `--allow-repeat` | 允许同一人跨奖品重复中奖（默认不允许） |
| `--dry-run` | 只预演不落盘 |
| `--out results` | 输出根目录 |

默认一人一票：**同一人回复 10 次，中奖概率和回复 1 次一样。**

---

## 过滤（`stats` 和 `draw` 都支持）

| 参数 | 作用 |
| --- | --- |
| `--exclude-op` | 剔除楼主的所有楼层（按 `thread.author.id` 精确匹配，不是只砍 1 楼） |
| `--floor-range 2-300` | 只算指定楼层区间（活动截止后的回复不算） |
| `--require-keyword KFC` | 回复里必须含关键词才算有效 |
| `--min-length 4` | 少于 N 个字不算（挡纯表情灌水） |
| `--blacklist black.txt` | 黑名单，一行一个用户名或 user_id |
| `--weight-by-replies` | 按回复次数加权（默认关闭，一般不要开） |

---

## 输出

```
results/20261103-0244_KFC150/
├── report.html          # 公示页：统计 + 名单 + @列表 + 种子/指纹，直接截图发贴
├── winners.csv          # 中奖明细（Excel 双击不乱码）
├── backup.csv           # 候补名单
├── participants.csv     # 去重后全部参与人，方便「我在不在名单里」自查
├── winners.md           # 一行 @ 名单 + 明细表，直接粘进贴吧
└── proof.json           # 全部参数 + seed + 三份 SHA-256
```

---

## 复核（给质疑的人看）

```bash
python3 lottery.py verify --proof results/xxx/proof.json --input data/replies.jsonl
```

```
✓ 输入指纹     e228db72d272177f…
✓ 名单指纹     305 人
✓ 结果指纹     1c18ed5b3f3b9ea0…
✓ 中奖名单     150 名
复核通过：数据、seed、结果三者完全一致。
```

随机数用自实现的 **SplitMix64 + Fisher–Yates**，不依赖 Python 版本；名单先按去重键排序再洗牌，
所以抓取顺序不影响结果。同一个 `seed` + 同一份数据 → 任何人任何机器重跑都是同一份名单。
有人事后改楼、删楼，`输入指纹` 会直接对不上。

---

## 常见问题

**Q：为什么按 `user_id` 去重，不按昵称？**
贴吧昵称可以重名、可以改；`user_id` 才是唯一身份。拿不到 id 时依次退到 `user_name` → `portrait` → `nickname`，`stats` 会把用了哪种键、各多少条打出来。

**Q：匿名抓不全怎么办？**
`r=1` 倒序通常能翻完；如果某页返回空，加 `--cookie-file`（BDUSS）再抓一次。

**Q：楼层有缺口？**
缺的是已删除 / 审核中 / 被折叠的楼层，接口本身不返回。实测 405 条、405 个唯一楼层，区间内少的 55 层全是这种情况（正序接口同样跳过）。

**Q：楼中楼算吗？**
不算，只统计主楼层。

**Q：去重后人数比奖品少？**
全体中奖 + 明确告警，`proof.json` 里记 `shortfall`，不会静默少发。

**Q：`403 百度安全验证`？**
网页 HTML 和旧接口会吃这个；`page_pc` 接口目前不受影响。真遇到就在 `--cookie-file` 带登录态。

---

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `lottery.py` | 主程序：`collect / stats / draw / verify` |
| `tieba_parse.py` | 解析与采集：`page_pc` 接口 / 网页 `data-field` / 纯文本 / CSV |
| `report.py` | 公示页渲染 |
| `docs/tieba-api.md` | 接口逆向笔记（签名、tbs、`r` 参数、字段映射） |
| `tests/` | 33 个单测，`python3 -m unittest discover -s tests` |

---

## 配套项目：瓜田雷达 🍉

同目录下的 [`吃瓜雷达/`](吃瓜雷达/README.md)：同一套贴吧接口做的**吃瓜监控台**。

盯帖抓新楼层 / 楼中楼 / 删楼 → 事件流 + 热度曲线 → 四种模板生成《吃瓜日报》
（本地规则引擎零配置可用，也能接大模型），报告页可直接存长图发帖。

```bash
cd 吃瓜雷达
python3 app.py --port 8765 --seed
```

它不依赖本项目，但可以 `POST /api/import` 直接吃这边抓出来的 `data/replies.jsonl`。
