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
- 接口：http://127.0.0.1:8787/api/feed?account=elonmusk&page=1&page_size=20

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

`--export` 按 `docs/data/accounts.json` 里的账号，把 SQLite 写成该账号自己的目录：

- `docs/data/accounts.json` — 盯盘账号（`handle`、显示名 `name`、`avatar`）
- `docs/data/<handle>/manifest.json`
- `docs/data/<handle>/page-1.json`、`page-2.json`、…

当前名单只有 `@elonmusk`，导出目录是 `docs/data/elonmusk/`。每页 JSON 与 `/api/feed` 同形（默认 20 条）。`docs/index.html` 先请求 `api/feed?account=<handle>`；没有这个接口时（即 github.io）就只读当前选中账号的分页文件。旧的扁平路径 `docs/data/manifest.json` + `docs/data/page-N.json` 仍可作为 `@elonmusk` 的后备。帖子内容没变时不写文件、也不产生空提交。

页面顶部用标签切换账号：名单前 4 个钉在横条上，其余进「更多」。现在只有一个账号，所以只有一个选中标签，「更多」不出现。上次选中的 handle 记在 `localStorage`（`musk-x-feed-account`）。

### 以后加第二个账号 / Adding another account

1. 在 `docs/data/accounts.json` 的 `accounts` 数组里追加一项，例如 `{"handle":"someuser","name":"Display Name","avatar":"https://..."}`。顺序就是标签顺序；第 5 个起进「更多」。不要在名单里放还没准备数据的占位账号。
2. 为该 handle 提供 `docs/data/<handle>/manifest.json` 和 `page-N.json`（形状与现有页相同）。页面不用改，它会只加载当前选中的账号。
3. 定时同步今天只抓 `@elonmusk`（xtracker + fxtwitter → `musk_feed.db`）。`export_pages.posts_for_handle` 只对这个 handle 返回数据库里的帖。别的 handle 在这里返回 `None`，导出不会覆盖你已经放好的静态文件。等抓取也接上之后，在 `posts_for_handle`（以及 `feed_core` 里的接口地址）加上该账号即可，不用重写页面。

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
