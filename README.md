# Civitai Post Splitter & Pixiv Uploader

[简体中文](#中文) · [English](#english)

把图片自动同步到 **Civitai / Pixiv / X (Twitter) / 小红书**。支持 Web UI 和 CLI 两种操作方式。

---

## 中文

### 功能

- **拆帖**：把 Civitai 一帖多图拆成多帖单图
- **多平台发布**：Civitai / Pixiv / X (Twitter) / 小红书，任意组合同时发布
- **自动 tag**：PixAI tagger（优先）或 WD14/CL tagger + Danbooru/Pixiv 翻译，自动转日文，角色/版权识别
- **LLM 文案反推**：接入 OpenAI 兼容 API，自动生成 Pixiv 标题简介 / X 推文 / 小红书笔记正文
- **R-18 自动打码**：YOLOv8 检测露出区域，mosaic / 高斯模糊 / 黑 bar 三选一，按平台合规线调档
- **Web UI**：浏览器操作，图片拖拽选取/排序，实时任务状态，Scheduler 配置
- **定时自动发布**：配置间隔范围后自动循环上传，Web UI 和 CLI 都支持
- **同图去重**：已成功发到某端的图，下次自动跳过该端
- **稳定性**：失败重试、连续失败中断、log 自动归档

### 环境要求

- Python 3.10+
- Chrome 浏览器（各平台发布由 Playwright 驱动）

### 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

R-18 打码（可选）：

```bash
pip install ultralytics opencv-python
```

YOLO 模型：双击 `run.bat` 选 [4] 自动下载，或手动从 [civitai.com/models/1736285](https://civitai.com/models/1736285?modelVersionId=1965032) 下载，放到 `models/auto_censor.pt`。

### 用法

#### CLI 菜单

双击 `run.bat` 或 `python launcher.py`：

```
[1] 拆分 Civitai 帖子（一帖多图 -> 多帖单图）
[2] 上传到双端 (Civitai + Pixiv)
[3] 仅上传到 Pixiv
[4] 安装 / 检查 R-18 自动打码
[5] 检查 / 拉取更新
[6] 配置 / 下载 Tagger 模型 (PixAI / CL)
[7] 切换 Pixiv 账号（清除 + 重新登录）
[8] 切换 Civitai 账号（清除 + 重新登录）
[9] 定时自动发布（配置 / 启动）
[Q] 退出
```

#### Web UI

```bash
python web_server.py
# 浏览器打开 http://localhost:7788
```

Web UI 支持：图片拖拽上传/手动排序、多平台勾选、LLM 反推配置、打码预设切换、Scheduler 实时状态。

#### 直接命令行

```bash
python civitai_splitter.py upload --targets civitai,pixiv,x,xhs --count 2
python civitai_splitter.py upload --targets pixiv --sort name_asc
```

`--targets` 支持：`civitai` / `pixiv` / `x` / `xhs`，逗号分隔任意组合。

`--sort`：`random`（默认）/ `name_asc` / `name_desc` / `time_asc` / `time_desc`

### 工作流

1. 把要发的图丢到 `upload/`
2. 用 Web UI 或 CLI 选择平台、发布
3. 成功的图移到 `done/`，失败的留在 `upload/` 等下次重发

### 平台说明

| 平台 | 发布方式 | 文案 | 打码 | NSFW |
|------|---------|------|------|------|
| Civitai | API + 浏览器 | — | — | ✓ |
| Pixiv | 浏览器（Playwright） | LLM 日文标题/说明 | ✓ | ✓ |
| X (Twitter) | 浏览器（Playwright） | LLM 英文推文 | ✓ | ✓ |
| 小红书 | 浏览器（Playwright） | LLM 中文标题/正文 | ✓ | **仅 SFW** |

小红书硬规则：r18/r18g 图自动跳过，不会发布。

### Tagger 配置

`run.bat` 选 [6] 进入 Tagger 配置菜单：

- **PixAI tagger v0.9**（推荐）：角色覆盖更广，能识别较新角色。需下载 `deepghs/pixai-tagger-v0.9-onnx`（约 1.27 GB）
- **CL / WD14 tagger**：轻量 fallback，兼容现有 WD14 ONNX 模型

优先级：PixAI > CL/WD14 > 仅 prompt/文件名候选

自动下载（需 `huggingface_hub`）：

```bash
pip install huggingface_hub
```

然后选菜单 [6] → [2] 自动下载。

### LLM 文案反推

连接 OpenAI 兼容 API（Claude / Gemini / GPT 等），为各平台生成文案：
- Pixiv：日文标题 + 简介
- X：英文推文（带 hashtag）
- 小红书：中文标题 + 正文

配置在 Web UI Settings 区或 `config.json.llm_reverse`。

### 配置文件

| 文件 | 作用 |
|---|---|
| `config.json` | 全局配置（API key、scheduler、LLM 反推、haintag_root）；本机私有，不提交 |
| `pixiv/censor.json` | 打码参数（preset: off / japan / strict，enabled_classes） |
| `pixiv/tag_aliases.json` | 自定义 tag 映射、drop_tags、语义组 |
| `pixiv/age_rules.json` | 文件名模式 → 年龄分级规则 |
| `pixiv/jp_aliases.json` | Danbooru→日文翻译缓存（自动累积） |
| `pixiv/general_jp.json` | Pixiv 规则表（mappings / selling_points / synonym_tags） |
| `x/x_templates.json` | X 发布模板（jp/en/zh × sfw/nsfw） |
| `x/cookies.json` | X 登录 cookie（Cookie-Editor 导出）；不提交 |
| `xhs/xhs_templates.json` | 小红书发布模板 |
| `xhs/cookies.json` | 小红书登录 cookie；不提交 |

### 许可

MIT

---

## English

### Features

- **Split**: turn multi-image Civitai posts into single-image reposts
- **Multi-platform publishing**: Civitai / Pixiv / X (Twitter) / Xiaohongshu (xhs), any combination
- **Auto-tagging**: PixAI tagger (preferred) or WD14/CL tagger + Danbooru/Pixiv translation; character/copyright tags auto-converted to Japanese
- **LLM copy generation**: OpenAI-compatible API generates Pixiv titles/captions, X tweets, xhs notes
- **R-18 auto-censor**: YOLOv8 detects exposed regions, applies mosaic / gaussian blur / black bar; compliance presets per platform
- **Web UI**: browser-based operation, drag-and-drop image selection/sorting, live task status, scheduler config
- **Scheduled publishing**: auto upload loop with configurable interval range
- **Per-image dedup**: already-published target is skipped on retry
- **Reliability**: auto-retry, consecutive-failure abort, log auto-archive

### Requirements

- Python 3.10+
- Chrome (platform publishing runs via Playwright)

### Install

```bash
pip install -r requirements.txt
playwright install chromium
```

Censoring (optional):

```bash
pip install ultralytics opencv-python
```

YOLO model: launch `run.bat` and pick [4] for auto-download, or manually fetch from [civitai.com/models/1736285](https://civitai.com/models/1736285?modelVersionId=1965032) to `models/auto_censor.pt`.

### Usage

#### CLI menu

Double-click `run.bat` or `python launcher.py`:

```
[1] Split a Civitai post (multi-image -> single-image posts)
[2] Upload to both (Civitai + Pixiv)
[3] Upload to Pixiv only
[4] Install / verify R-18 auto-censor
[5] Check / pull updates
[6] Configure / download Tagger model (PixAI / CL)
[7] Switch Pixiv account (clear + re-login)
[8] Switch Civitai account (clear + re-login)
[9] Scheduled auto-publish (configure / start)
[Q] Quit
```

#### Web UI

```bash
python web_server.py
# open http://localhost:7788 in browser
```

#### Direct CLI

```bash
python civitai_splitter.py upload --targets civitai,pixiv,x,xhs --count 2
python civitai_splitter.py upload --targets pixiv --sort name_asc
```

`--targets`: `civitai` / `pixiv` / `x` / `xhs`, comma-separated, any combination.

`--sort`: `random` (default) / `name_asc` / `name_desc` / `time_asc` / `time_desc`

### Workflow

1. Drop images into `upload/`
2. Select platforms and publish via Web UI or CLI
3. Successful images move to `done/`; failed ones stay in `upload/` for retry

### Platform notes

| Platform | Method | Copy | Censor | NSFW |
|----------|--------|------|--------|------|
| Civitai | API + browser | — | — | ✓ |
| Pixiv | Playwright | LLM JP title/caption | ✓ | ✓ |
| X (Twitter) | Playwright | LLM EN tweet | ✓ | ✓ |
| Xiaohongshu | Playwright | LLM ZH title/body | ✓ | **SFW only** |

xhs hard rule: r18/r18g images are automatically skipped.

### Tagger setup

Run `[6]` from the launcher menu:

- **PixAI tagger v0.9** (recommended): broader character coverage including newer characters. Requires `deepghs/pixai-tagger-v0.9-onnx` (~1.27 GB)
- **CL / WD14 tagger**: lighter fallback, compatible with existing WD14 ONNX models

Priority: PixAI > CL/WD14 > prompt/filename candidates only

Auto-download (requires `huggingface_hub`):

```bash
pip install huggingface_hub
```

Then pick menu `[6]` → `[2]`.

### LLM copy generation

Connects to an OpenAI-compatible API to generate:
- Pixiv: Japanese title + caption
- X: English tweet with hashtags
- xhs: Chinese title + body

Configure in Web UI Settings or `config.json.llm_reverse`.

### License

MIT
