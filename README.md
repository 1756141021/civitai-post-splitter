# Civitai Post Splitter & Pixiv Uploader

[简体中文](#中文) · [English](#english)

把图片自动同步到 **Civitai / Pixiv / X (Twitter) / 小红书**。支持 Web UI 和 CLI 两种操作方式。

不推荐使用发布到小红书功能，小红书的检测和风控特别严格，容易坠机

---

## 中文

### 功能

- **拆帖**：把 Civitai 一帖多图拆成多帖单图
- **多平台发布**：Civitai / Pixiv / X (Twitter) / 小红书，任意组合同时发布
- **自动 tag**：PixAI tagger（优先）或 WD14/CL tagger + Danbooru/Pixiv 翻译，自动转日文，角色/版权识别
- **LLM 文案反推**：接入 OpenAI 兼容 API，自动生成 Pixiv 标题简介 / X 推文 / 小红书笔记正文
- **R-18 自动打码**：YOLOv8 检测露出区域，mosaic / blur / bar / heart 四种遮挡方式，off / japan / strict 三档合规线
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
patchright install chromium
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
python civitai_splitter.py split 1234567          # 拆分指定 post（多个 ID 用空格隔开）
```

`upload` 常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--targets` | `civitai` | 发布目标，逗号分隔：`civitai` / `pixiv` / `x` / `xhs` |
| `--count` | `0` | 本次发几张，`0` = 随机 1–5 |
| `--sort` | `random` | 选图排序：`random` / `name_asc` / `name_desc` / `time_asc` / `time_desc` |
| `--dry-run` | 关 | 只生成 manifest 和清洗副本，不实际发布 |
| `--x-template` | `en_sfw` | X 模板：`jp/en/zh` × `sfw/nsfw`；r18/r18g 自动切到 `*_nsfw` |
| `--x-group` | `1` | X 多图组队（1=每图单推；2–4=按文件名相邻组队，一条推挂多图） |
| `--xhs-template` | `default` | 小红书模板 |
| `--xhs-manual` | 关 | 小红书手动模式：只生成内容，不启动浏览器 |
| `--no-ai-tags` | 关 | 不打 AI 标签；不带值=全部平台，带值=指定平台（如 `pixiv,x`） |
| `--pixiv-privacy` | `public` | `public` / `logged_in` / `mypixiv` / `private` |
| `--abort-after-failures` | `3` | 连续失败 N 张后中断批次，避免风控 |
| `--delay` | `10` | 每个 post 间隔秒数 |
| `--llm-reverse` | 关 | 用 LLM 生成文案；配 `--llm-persona` / `--llm-account` / `--llm-content-mode` |

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

### R-18 打码

两个独立旋钮，配在 `pixiv/censor.json`，Web UI Settings 也能切：

**遮挡方式**（`mode`）：

| mode | 效果 |
|---|---|
| `mosaic`（默认） | 马赛克 |
| `blur` | 高斯模糊 |
| `bar` | 黑条（`bar_count` 控制条数） |
| `heart` | 爱心遮挡 |

**合规档位**（`preset` / `enabled_classes`）：决定遮哪些部位。

| preset | 遮挡范围 | 含义 |
|---|---|---|
| `off` | 无 | 不打码 |
| `japan`（默认） | `dick / vagina / anus / cum` | Pixiv 平台合规线（生殖区域 + 体液，不含乳头） |
| `strict` | 再加 `tits` | 加乳头 |

`conf_threshold` 控制检测灵敏度，推荐 0.5–0.6。切换 preset 无需重启 Web 服务即时生效。

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

### 登录与账号

每个平台独立 Chrome profile，互不干扰：

- Pixiv：`~/.civitai_splitter_pixiv_chrome`
- Civitai：`~/.civitai_splitter_chrome`（登录走 `civitai.red`，发布后 URL 可能显示 `civitai.com`，正常）
- X：`~/.civitai_splitter_x_chrome`
- 小红书：`~/.civitai_splitter_xhs_chrome`

X 和小红书还支持 **cookie 导入**：自动化浏览器会被 Google 登录拦，所以从普通 Chrome 用 Cookie-Editor 扩展导出 JSON，放到 `x/cookies.json` 或 `xhs/cookies.json`，启动时自动注入。两个文件都在 `.gitignore` 里——`auth_token` + `ct0` 等于账号完全访问权，绝不能提交。

launcher 菜单 `[7]` / `[8]` 清除 Pixiv / Civitai profile 并重新登录。

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
- **R-18 auto-censor**: YOLOv8 detects exposed regions; mosaic / blur / bar / heart masking; off / japan / strict compliance presets
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
patchright install chromium
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
python civitai_splitter.py split 1234567          # split a specific post (space-separate multiple IDs)
```

Common `upload` flags:

| Flag | Default | Description |
|---|---|---|
| `--targets` | `civitai` | Publish targets, comma-separated: `civitai` / `pixiv` / `x` / `xhs` |
| `--count` | `0` | How many to publish; `0` = random 1–5 |
| `--sort` | `random` | Pick order: `random` / `name_asc` / `name_desc` / `time_asc` / `time_desc` |
| `--dry-run` | off | Build manifest + sanitized copies only, don't publish |
| `--x-template` | `en_sfw` | X template: `jp/en/zh` × `sfw/nsfw`; r18/r18g auto-switches to `*_nsfw` |
| `--x-group` | `1` | X multi-image grouping (1=one post per image; 2–4=group adjacent files into one post) |
| `--xhs-template` | `default` | Xiaohongshu template |
| `--xhs-manual` | off | xhs manual mode: generate content only, don't launch browser |
| `--no-ai-tags` | off | Skip AI tags; no value=all platforms, value=specific platforms (e.g. `pixiv,x`) |
| `--pixiv-privacy` | `public` | `public` / `logged_in` / `mypixiv` / `private` |
| `--abort-after-failures` | `3` | Abort the batch after N consecutive failures (anti rate-limit) |
| `--delay` | `10` | Seconds between posts |
| `--llm-reverse` | off | Generate copy via LLM; pair with `--llm-persona` / `--llm-account` / `--llm-content-mode` |

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

### R-18 censoring

Two independent knobs in `pixiv/censor.json` (also switchable from Web UI Settings):

**Masking method** (`mode`):

| mode | Effect |
|---|---|
| `mosaic` (default) | Mosaic |
| `blur` | Gaussian blur |
| `bar` | Black bars (`bar_count` controls how many) |
| `heart` | Heart overlay |

**Compliance preset** (`preset` / `enabled_classes`) — which regions get covered:

| preset | Coverage | Meaning |
|---|---|---|
| `off` | none | No censoring |
| `japan` (default) | `dick / vagina / anus / cum` | Pixiv compliance line (genitals + fluids, nipples excluded) |
| `strict` | adds `tits` | Adds nipples |

`conf_threshold` tunes detection sensitivity (0.5–0.6 recommended). Switching preset takes effect without restarting the web server.

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

### Login & accounts

Each platform gets its own isolated Chrome profile:

- Pixiv: `~/.civitai_splitter_pixiv_chrome`
- Civitai: `~/.civitai_splitter_chrome` (login via `civitai.red`; published URLs may show `civitai.com` — normal)
- X: `~/.civitai_splitter_x_chrome`
- Xiaohongshu: `~/.civitai_splitter_xhs_chrome`

X and xhs also support **cookie import**: automated browsers get blocked at Google login, so export JSON from a normal Chrome with the Cookie-Editor extension into `x/cookies.json` or `xhs/cookies.json` — injected automatically on launch. Both are `.gitignore`d — `auth_token` + `ct0` equals full account access, never commit them.

Launcher menu `[7]` / `[8]` clears the Pixiv / Civitai profile and re-logs in.

### License

MIT
