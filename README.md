# 马斯克 X 盯盘 / Elon Musk X Feed (GitHub Pages)

静态站点：最近 3 天 `@elonmusk` 帖子卡片。定时用 GitHub Actions 拉取并提交到 `docs/`，由 GitHub Pages 托管。

Static site: last 3 days of `@elonmusk` posts as cards. GitHub Actions fetches on a schedule, commits into `docs/`, and GitHub Pages serves it.

## 预期地址 / Expected URL

```
https://<your-username>.github.io/<repo-name>/
```

例如 / e.g. `https://octocat.github.io/musk-pages/`

## 启用步骤 / Setup

### 1. 推送到 GitHub / Push to GitHub

```bash
cd musk-pages
git init
git add .
git commit -m "Initial Elon Musk X feed site"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

### 2. 开启 GitHub Pages（从 docs/）/ Enable Pages from docs/

1. Repo → **Settings** → **Pages**
2. **Source**: Deploy from a branch
3. **Branch**: `main` / folder **`/docs`**
4. Save — 几分钟后访问上面的 URL

### 3. Actions 写权限 / Actions write permission

定时任务需要把更新后的 `docs/` 推回仓库：

1. Repo → **Settings** → **Actions** → **General**
2. **Workflow permissions** → 选择 **Read and write permissions**
3. Save

也可在首次失败后按提示授权；本仓库 workflow 已声明 `permissions: contents: write`。

### 4. 手动跑一次 / Manual run

**Actions** → **Update Musk X Feed** → **Run workflow**

之后默认每约 10 分钟（`*/10 * * * *`）自动更新；无内容变化时不产生空提交。

## 本地构建 / Local build

```bash
python3 scripts/build_feed.py
# 打开 docs/index.html，或:
python3 -m http.server 8080 --directory docs
```

## 说明 / Notes

| 项 | 内容 |
|---|---|
| 数据源 | xtracker.polymarket.com（列表）+ api.fxtwitter.com（enrich 前 ~25 条） |
| 时区 | Asia/Shanghai（CST） |
| UI | 中文界面文案；帖子正文保持英文原样 |
| 图片 | 下载到 `docs/media/`；失败则保留远程 URL 并标记 |
| 跳过空提交 | 仅当帖子内容指纹变化时才 `git commit` + `push` |

仅供个人盯盘，请遵守各 API / X 使用条款。
