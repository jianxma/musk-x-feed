# 马斯克 X 盯盘 / Elon Musk X Feed

最近约 3 天 `@elonmusk` 的帖子。本地用 **SQLite** 增量存储；GitHub Pages 只托管从数据库导出的分页 JSON 和一套时间线页面。

Recent `@elonmusk` posts. Locally this is a **SQLite** incremental feed. GitHub Pages serves a static export of that database (paginated JSON + one timeline page).

界面文案是中文，帖子正文保持英文。卡片按 X 时间线排：约 600px 宽、扁分隔线、头像 / 认证标 / 相对时间、转发上下文、嵌套引用、1–4 图、回复 / 转发 / 喜欢 / 书签 / 查看。

## 本地运行 / Run locally

不需要 pip（Python 3.10+，标准库 + sqlite3）。

```bash
python3 sync.py          # 拉新帖，只 enrich 尚未补全的（默认最多 20 条）
python3 app.py           # http://127.0.0.1:8787/
```

或 `./serve.sh`。端口用环境变量 `PORT`（默认 `8787`）。`SYNC_INTERVAL=0` 可关掉进程内的后台同步（默认约 90 秒一次）。

- 页面：http://127.0.0.1:8787/
- 接口：http://127.0.0.1:8787/api/feed?page=1&page_size=20

```json
{
  "posts": [],
  "page": 1,
  "page_size": 20,
  "total": 0,
  "total_pages": 1,
  "updated_at_shanghai": "2026-09-23 14:00:00"
}
```

`sync.py` 只写 `musk_feed.db`，不重写 HTML。空库时会从 `docs/feed.json` 灌入一次历史帖。已补全的帖不会被一次只有正文的列表覆盖。

正文会把连续 3 个以上换行收成最多 2 个；页面用 `white-space: pre-wrap`，不会再把换行换成 `<br>`。

图片用远程 `pbs.twimg.com` 地址，不再下载到 `docs/media/`。

## GitHub Pages 上是什么 / What Pages serves

Pages 不能跑 Python。Actions 定时执行：

```bash
python3 sync.py --export
```

`--export` 把 SQLite 写成：

- `docs/data/manifest.json`
- `docs/data/page-1.json`、`page-2.json`、…

每页 JSON 与 `/api/feed` 同形（默认 20 条）。`docs/index.html` 先请求 `api/feed`；没有这个接口时（即 github.io）就按页读这些文件。帖子内容没变时不写文件、也不产生空提交。

数据库 `musk_feed.db` 提交在仓库根目录，这样下一次 Actions 能接着增量 enrich，而不是每次把站点整页重做一遍。

### 开启 Pages / Enable Pages

1. **Settings → Pages → Build and deployment**
2. Source: **Deploy from a branch**
3. Branch: `main`，文件夹 **`/docs`**
4. **Settings → Actions → General → Workflow permissions**: **Read and write**

地址：`https://jianxma.github.io/musk-x-feed/`

手动跑：**Actions → Update Musk X Feed → Run workflow**。定时为每约 10 分钟（`*/10 * * * *`，Actions 会有几分钟偏差）。

只想根据当前数据库重导出、不拉网络：

```bash
python3 export_pages.py
```

`scripts/build_feed.py` 仍可用，它现在只是 `sync` + `export`，不再生成一整份静态 HTML。

## 分类 / Classification

| 类型 | 判定 |
|---|---|
| 转发 | fxtwitter 返回 `reposted_by`，或作者不是 `@elonmusk`，或旧字段 `retweet` |
| 引用 | 马斯克自己的帖子里带 `quote` |
| 原文 | 其余 |

转发卡片上方是「Elon Musk 转发了」，链接到 **马斯克自己的** status（`https://x.com/elonmusk/status/{platformId}`）。下面嵌套原作者、正文和媒体。引用用带边框的嵌套卡片，引用里的图片留在卡片内。

数据来自 xtracker.polymarket.com（列表）和 api.fxtwitter.com（补全）。时区 Asia/Shanghai。单次 enrich 失败会跳过，下次再试。仅供个人盯盘，请遵守各 API / X 的使用条款。
