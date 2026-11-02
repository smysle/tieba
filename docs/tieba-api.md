# 贴吧网页端接口逆向笔记

目标：不靠浏览器、不靠 Selenium/无头 Chrome，纯 HTTP 拿到一个帖子的全部楼层。
样本帖：`https://tieba.baidu.com/p/11154226751`

---

## 1. 页面是 SPA，HTML 里没有数据

`GET https://tieba.baidu.com/p/<tid>` 返回的是 11KB 的空壳（Vue/React SPA），
所有楼层靠 XHR 拉。curl 直连还大概率吃 `403 百度安全验证`。
所以只能打接口。

## 2. 接口清单

| 用途 | 方法 | 地址 |
| --- | --- | --- |
| 取 tbs | GET | `https://tieba.baidu.com/c/s/pc/sync` |
| 帖子楼层 | POST | `https://tieba.baidu.com/c/f/pb/page_pc` |

### 2.1 先拿 tbs

```
GET /c/s/pc/sync?subapp_type=pc&_client_type=20&sign=<sign>
→ {"data": {"anti": {"tbs": "075a8955fadbf6931793644912"}, "user": {"is_login": 0}}}
```

`tbs` 会过期（重放旧 tbs 报 `110001 未知错误`），**每次批量抓取前重新 sync 一次**。

### 2.2 楼层接口

```
POST /c/f/pb/page_pc
Content-Type: application/x-www-form-urlencoded;charset=UTF-8
x-requested-with: XMLHttpRequest

pn=1&lz=0&r=1&mark_type=0&back=0&fr=frs&kz=<tid>
  &session_request_times=1&tbs=<tbs>&subapp_type=pc&_client_type=20&sign=<sign>
```

响应里真正有用的字段：

```json
{
  "page":      {"current_page": 1, "total_page": 28, "has_more": 1},
  "thread":    {"id": 11154226751, "reply_num": 623, "title": "…", "author": {"id": 1269140530, "name": "蛋芽"}},
  "post_list": [{"id": 154099660399, "floor": 461, "author_id": 3546634041,
                 "time": 1793631560, "content": [{"type": 0, "text": "睡网吧谁能绷住"}],
                 "sub_post_list": {"sub_post_list": [...]}}],
  "user_list": [{"id": 3546634041, "name": "…", "name_show": "…", "portrait": "tb.1.xxx",
                 "level_name": "二年级", "ip_address": "广东"}]
}
```

- `post_list[].author_id` 只有数字 ID，**名字在 `user_list` 里**，按 `id` 关联。
- `content` 是分段数组（`type:0` 文本、`type:3` 图片），要拼起来。
- `sub_post_list` 是楼中楼，抽奖不计。
- `thread.author.id` 是楼主 ID —— 用它可以精确排除楼主的所有回复（不止 1 楼）。

## 3. 签名算法（关键）

在 `tb3.bdstatic.com/tb/pc/pc-common/static/js/base.*.js` 里，压缩后的核心就这几行：

```js
i = subapp_type === 'client' ? 'tiebaclient!!!'
  : subapp_type === 'pc'     ? '36770b1f34c9bbf2e7d1a99d2b82fa9e'
  :                            '0039d79dc3cc2075129745a30237a3c4';
r = Object.keys(data).sort((a, b) => a > b ? 1 : -1);
r.forEach(k => { n += k + '=' + data[k]; });
n += i;
sign = md5(n);
```

也就是：

```
sign = md5( "".join(f"{k}={v}" for k in sorted(params)) + SALT )
```

- PC 盐：`36770b1f34c9bbf2e7d1a99d2b82fa9e`
- 客户端盐：`tiebaclient!!!`
- 排序是 JS 默认字符串排序（`_client_type` 因为 `_` 的码点小于小写字母，排在 `back` 之前 —— Python 的 `sorted()` 结果一致）

验证：抓包得到的 `sign=e4e93d5905a2b4a311d495189355c484`，用上面的算法可以逐字节复现
（见 `tests/test_lottery.py::PcApiSignTest`）。

## 4. `r` 参数是个坑

| r | 行为 |
| --- | --- |
| `r=2` | 正序（旧→新）。匿名时 `pn≥2` 直接返回 `post_list: []` —— 就是页面上那句「登录后查看全部评论内容」 |
| `r=1` | 倒序（新→旧）。**匿名也能一页页翻到底**，共 `total_page` 页 |

所以拿全量不需要登录：从 `pn=1` 开始用 `r=1` 一直翻到 `total_page`，
落地后按 `floor` 重新排序即可。

## 5. 覆盖率（怎么证明抓全了）

样本帖 `total_page=28`、每页 15 条 → 实测 27 页有效、405 条，楼层 2–461，楼层号零重复。

四路交叉验证，结论是**全了**，区间内少的 55 层是服务端本来就不返回的：

| 验证 | 结果 |
| --- | --- |
| 对账 `thread.reply_num` | 405 主楼层 + 215 条楼中楼 = 620，对 623，差 3（已删除） |
| 正序 `r=2` 第 1 页 | 返回 2,3,4,7,8,9,11,12,13,14,15,16,17,18 —— **同样跳过 5/6/10**，说明缺口在服务端 |
| 越界翻页 | `pn=28~32` 全空、`has_more=0`，最后一页落在 2 楼，没有漏页 |
| 楼层连续性 | 405 条 = 405 个唯一楼层，无重复、无缺页拼接错误 |

`total_page=28` 比实际页数多 1（末尾空页），所以循环里用 `got == 0` 收尾。

## 6. 什么时候仍然需要 BDUSS

- 想拿 `r=2` 正序分页（有些老帖 `r=1` 会被截断）
- 想看被折叠楼层、被删楼层、楼中楼全量
- 出现 `403 百度安全验证` 的频率变高时，带登录态最稳

`lottery.py` 里用 `--cookie-file data/raw/cookie.txt`（内容 `BDUSS=xxxx` 或裸值）即可。

## 7. 落到代码

`tieba_parse.py`：

```python
tp.pc_sign(params)                      # 签名
tp.pc_sync_tbs(tid, cookie)             # 取 tbs
tp.pc_page(tid, pn, tbs, cookie, r=1)   # 取一页
tp.fetch_pc_api(tid, cookie, pages)     # 全量翻页 + user_list 关联 + 去重
```

命令行：

```bash
python3 lottery.py collect --tid 11154226751 --out data/replies.jsonl
```
