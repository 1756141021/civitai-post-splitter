# Civitai Post Splitter & Pixiv Uploader

[简体中文](#中文) · [English](#english)

把 Civitai 多图帖子拆成单图，自动同步到 Pixiv。带 R-18 自动打码、智能 tag 翻译、失败重试 / 同图去重 / 限速。

---

## 中文

### 功能

- **拆帖**：把 Civitai 一帖多图拆成多帖单图
- **双端发布**：同时发到 Civitai + Pixiv
- **自动 tag**：本地 WD14 风格图像识别 + Danbooru/Pixiv 翻译，character/copyright 自动转日文
- **R-18 自动打码**：YOLOv8 检测露出区域，自动 mosaic / 高斯模糊 / 黑 bar 三选一
- **同图去重**：已成功发到某端的图，下次跑自动跳过该端，避免重复发布
- **稳定性**：失败自动重试、连续失败中断、log 自动归档

### 环境要求

- Python 3.10+
- Chrome 浏览器（Pixiv 上传由 playwright 驱动）

### 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

R-18 打码功能需要额外依赖（可选）：

```bash
pip install ultralytics opencv-python
```

并下载 YOLO 模型：双击 `run.bat` 选 [4] 自动下载，或手动从 [civitai.com/models/1736285](https://civitai.com/models/1736285?modelVersionId=1965032) 下载，放到 `models/auto_censor.pt`。

### 用法

双击 `run.bat`，选菜单：

```
[1] 拆分 Civitai 帖子（一帖多图 -> 多帖单图）
[2] 上传到双端 (Civitai + Pixiv)
[3] 仅上传到 Pixiv
[4] 安装 / 检查 R-18 自动打码
```

工作流：

1. 用 [1] 拆 Civitai 帖子，结果会落到 `done/`
2. 把要发的图丢到 `upload/`
3. 用 [2] 或 [3] 发布
4. 成功的图自动移到 `done/`，失败的图留在 `upload/` 等下次重发

### 配置

第一次跑会自动生成下面这些 json，编辑保存即可生效：

| 文件 | 作用 |
|---|---|
| `pixiv_censor.json` | 打码参数（mode/阈值/类别） |
| `pixiv_tag_aliases.json` | 自定义 tag 映射 / 屏蔽词 |
| `pixiv_age_rules.json` | 文件名 → 年龄分级规则 |
| `pixiv_jp_aliases.json` | Danbooru→日文翻译缓存（自动累积） |

### 许可

MIT

---

## English

### Features

- **Split**: turn multi-image Civitai posts into single-image reposts
- **Dual publishing**: post to Civitai + Pixiv simultaneously
- **Auto-tagging**: local WD14-style image classifier + Danbooru/Pixiv translation; character/copyright tags auto-converted to Japanese
- **R-18 auto-censor**: YOLOv8 detects exposed regions, applies one of mosaic / gaussian blur / black bar
- **Per-image dedup**: already-published target is skipped on retry, no duplicate posts
- **Reliability**: auto-retry, consecutive-failure abort, log auto-archive

### Requirements

- Python 3.10+
- Chrome (Pixiv upload runs via playwright)

### Install

```bash
pip install -r requirements.txt
playwright install chromium
```

Censoring needs extras (optional):

```bash
pip install ultralytics opencv-python
```

Plus the YOLO model: launch `run.bat` and pick [4] for auto-download, or manually fetch from [civitai.com/models/1736285](https://civitai.com/models/1736285?modelVersionId=1965032) to `models/auto_censor.pt`.

### Usage

Double-click `run.bat` and pick from the menu:

```
[1] Split a Civitai post (multi-image -> single-image posts)
[2] Upload to both (Civitai + Pixiv)
[3] Upload to Pixiv only
[4] Install / verify R-18 auto-censor
```

Workflow:

1. Use [1] to split Civitai posts; results land in `done/`
2. Drop images you want to publish into `upload/`
3. Use [2] or [3] to publish
4. Successful images move to `done/`; failed ones stay in `upload/` for retry

### Configuration

These json files auto-generate on first run; edit them and rerun:

| File | Purpose |
|---|---|
| `pixiv_censor.json` | censor params (mode / threshold / classes) |
| `pixiv_tag_aliases.json` | custom tag mappings / blocklist |
| `pixiv_age_rules.json` | filename → age-rating rules |
| `pixiv_jp_aliases.json` | Danbooru→Japanese translation cache (auto-accumulates) |

### License

MIT
