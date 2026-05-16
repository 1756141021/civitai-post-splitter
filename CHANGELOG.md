# Changelog

## 2026-05-16

### Added
- **PixAI Tagger v0.9 集成**：新建 `pixiv/pixai_tagger.py`（`PixAITaggerBridge`），支持 `deepghs/pixai-tagger-v0.9-onnx` ONNX 模型。CLIP 风格归一化（mean=std=0.5，BCHW layout），读取 `preprocess.json` / `thresholds.csv` / `selected_tags.csv`，返回与 `StandaloneTaggerBridge` 相同 schema（含空 `copyright` 组保持接口兼容）。
- **Tagger 优先链**：`_make_bridges()` 拆分 metadata reader 和 tagger bridge 选择逻辑。新优先链：PixAI → CL/WD14 → None，HainTag 退出 tagger 自动优先（metadata reader 保持不变）。
- **Launcher 下载菜单**：`[6]` 改为「配置 / 下载 Tagger 模型」，进入子菜单可选：配置向导 / 自动下载 PixAI（`huggingface_hub.snapshot_download`）/ 查看手动下载地址。
- **Setup wizard 支持双 tagger**：`setup_tagger.py` 新增 PixAI 配置步骤（`step2_pixai_model_dir` / `step3_pixai_verify`），`main()` 现在询问配置 PixAI / CL / 两者，互不干扰。
- **Manifest `tagger_type` 字段**：manifest `pixiv.tagger` 新增 `tagger_type: "pixai"|"cl"`，旧 manifest 向前兼容（key 缺失默认 cl）。

### Fixed
- `_write_haintag_settings`（`setup_tagger.py` 和 `launcher.py`）：`else` 分支改为 `existing.update(settings)`，修复 fresh 用户（无 HainTag `{"settings":{}}` 嵌套格式）配置多个 key 时后写覆盖前写的问题。预存在问题，PixAI + CL 双配置时首次暴露。

## 2026-05-15

### Fixed
- 账号面板登录状态判断：Pixiv / Civitai / 小红书 改回"profile 目录是否存在"判断，与登录前的老逻辑一致。原 `.session_valid` 标记文件方案依赖 `while context.pages` 循环结束后写文件，浏览器关闭时 `context.pages` 访问抛异常会被外层 `except` 吞掉，touch 永远跑不到，导致登录完状态不更新。

### Removed
- `.session_valid` 标记文件相关读/写/删全部清理（dead code）。

## 2026-05-14 (4)

### Added
- **X (Twitter) publishing**: new `x/` module mirrors pixiv module shape. Playwright over persistent profile with cookies.json import (Cookie-Editor JSON), stealth init script for X automation-detection bypass, Ctrl+Enter post shortcut, sensitive-media auto-label hook. Default template `en_sfw` per X traffic research (2 hashtags sweet spot, entity + template core).
- **小红书 (xhs) publishing**: new `xhs/` module. Web 版 publishing flow (`creator.xiaohongshu.com/publish/publish`) with dropdown-driven topic insertion (xhs requires picking topics from suggestion list, plain `#xxx` text isn't registered as a topic). Auto-ticks AI synthesis declaration checkbox per GB45438-2025 compliance. NSFW images (r18/r18g) are hard-rejected before publish.
- **Universal `manifest.copy` area**: LLM reverse output now lives in `manifest.copy.{title,caption}.{ja,en,zh}` so X/xhs/pixiv all read from one place. Pixiv legacy fields kept for back-compat. New `apply_llm_result_to_copy_block` projects platform-specific LLM fields onto the universal area.
- **`PLATFORM_RULES` table** in `civitai_splitter.py`: drives per-platform `needs_sanitize`, `needs_censor`, `needs_copy`, `max_age`. Replaces the old `needs_pixiv_pipeline = pixiv or x` hack. civitai stays image-only (no sanitize/censor/LLM); pixiv/x/xhs share the sanitized artifact.
- **LLM reverse account `max_nsfw_level`** (sfw / r18 / r18g): per-account NSFW capability flag. cmd_upload skips reverse when image age exceeds account ceiling instead of asking the API to look at content it'll refuse.
- **LLM reverse on-demand**: only runs when targets include a platform that needs copy (civitai-only uploads skip the LLM call entirely).
- **Censor preset system** (`pixiv/censor.json` `preset` field): `off` / `japan` (Pixiv 标准) / `strict` levels. Default `japan` covers genital area + bodily fluids (no nipples — matches Pixiv platform compliance). `strict` adds nipples. UI label displays "Pixiv 标准" for the `japan` preset. New `/api/censor-preset` endpoint and Web UI selector under settings.
- **Web UI multi-target selector**: `ImagePickerDialog` and `SchedulerDialog` now show 4 checkboxes (Civitai / Pixiv / X / 小红书). Selection persisted to `localStorage` under `civitai-splitter:upload-targets`.

### Changed
- `TARGETS` set in `civitai_splitter.py` now includes `x` and `xhs`. `--targets` accepts these.
- `cmd_upload` web-server bridge (`web_server.py`) `cmd=2` / `cmd=3` paths consolidated into one upload entry; targets are driven by `params.targets` instead of hardcoded per-cmd defaults.
- `pixiv/support.py` `build_pixiv_payload` now exposes `entity_tags` (character / copyright / franchise / identity tags as a flat list) so platform modules can pick them without parsing the full payload.

### Removed
- `x-collect-tags` subcommand and the `hot_tags.json` / `hot_tags_auto.json` / Danbooru reverse-index machinery in `x/support.py`. Replaced by a 2-tag picker driven by template `core` + `social` fields, matching the X 2-hashtag sweet-spot research (3+ tags drop engagement by 17%).

## 2026-05-14 (3)

### Fixed
- `HainTagTaggerBridge._load_settings()` now falls back to root-level JSON keys when no `settings` wrapper exists, matching all other settings loaders. This prevents tagger `model_dir` from silently reading as empty when the path was saved via the Web UI.
- Added `_model_dir` property to `HainTagTaggerBridge` so the tagger-probe check in `cmd_upload` no longer misreports "未配置 model_dir" when haintag is installed.
- "随机 1-5" button in `ImagePickerDialog` now passes the current sort mode to the backend, so it picks from the most-recently-modified (or name-sorted) images when a non-random sort is active.

### Added
- Main web page now accepts file drag-and-drop directly (no need to open ImagePickerDialog): dragging images from Explorer onto the browser window shows a blue overlay and saves files to `upload/` via `/api/add-upload-files`; a toast confirms the count added.

## 2026-05-14 (2)

### Added
- Added `--sort` parameter to `upload` command: `random` (default), `name_asc`, `name_desc`, `time_asc`, `time_desc`. Specifying a count now reliably picks the first N images by the chosen rule instead of random sampling.
- Added manual drag-and-drop ordering in the Web UI image picker (`手动排序` mode): images reordered via drag handles; unselected images shown below for one-click append.
- CLI `_ask_upload_params` now prompts for sort order after count.
- Scheduler config includes a `sort` field persisted to `config.json`; timed uploads use the saved rule. Old configs without `sort` transparently default to `random`.
- `/api/images` now returns `mtime` for client-side time-based sorting.

## 2026-05-14

### Added
- Added Pixiv LLM reverse inference for generating title and caption copy through an OpenAI-compatible vision API.
- Added persona/account configuration with SFW/NSFW content modes for Pixiv copy generation.
- Added Web UI controls for configuring LLM reverse inference and enabling it during Pixiv upload selection.

### Changed
- Pixiv upload manifests now record LLM reverse inference status, selected persona/account, content mode, generated copy, and fallback errors without exposing API keys.

## 2026-05-13

### Added
- Pixiv tag generation now records `metadata_entity_hits` in manifests and rule-fit compare output so metadata-derived fanart detections are visible during tuning.

### Changed
- Pixiv tag generation now recognizes Danbooru-style metadata entities such as `hatsune miku`, `devil janai mon \(vocaloid\)`, and known franchise tags, then routes them through the existing Danbooru→Pixiv JP mapping chain.
- Pixiv fanart tag ordering now places作品 and角色 tags before required and generic tags so prompt-derived entities are not pushed out by the 10-tag cap.
- Metadata-driven fanart detection now stays strict to character/franchise-shaped tokens and no longer promotes generic feature tags like hair color or clothing details into character entities.

## 2026-05-12

### Fixed
- Web UI task cancel now propagates into launcher update checks and upload internals instead of waiting until the whole command returns, so queued/setup/update/upload tasks stop sooner and finish in a real `canceled` state.
- Civitai and Pixiv publish flows now keep cancellation cooperative before the irreversible publish click, but stop rewriting successful post-click completion into a misleading canceled result.

## 2026-05-11

### Fixed
- Pixiv tag generation now maps generic `dress` to `ドレス` instead of ambiguous `ワンピース`, preventing Pixiv from resolving the tag to `ONE PIECE`.

## 2026-05-10

### Added
- Added scheduler support in both the launcher and Web UI, including persisted interval/count/target settings and live scheduler status updates.
- Added browser shutdown handling from the Web UI so the local server can stop itself after the page closes and active tasks finish.
- Added Pixiv rule-fit tooling for collecting high-traffic Pixiv samples, downloading reference images, comparing local generated tags against Pixiv tags, and writing summary reports.
- Added `synonym_tags` to Pixiv general tag configuration so canonical tags can also emit high-value aliases such as BlueArchive / Arknights / WutheringWaves forms.
- Added expanded Pixiv tag mappings, selling-point rules, alias overrides, popularity data, and validation cases.

### Changed
- Pixiv tag generation now keeps generic VTuber tags only when WD14 also identifies a specific character.
- Pixiv tag generation now groups standard WD14/Danbooru tags into Pixiv-native candidate sets, rejects ambiguous one-piece terms without source proof, and ranks content tags by Pixiv popularity counts instead of tagger score proxies.
- Pixiv age detection now promotes explicit adult candidates such as nipples / pussy / pubic hair to R-18 and keeps the R-18 tag synchronized when censoring forces the rating.
- Pixiv tag generation now gives WD14 tagger output stricter category-aware handling, uses the 151k Danbooru→JP table behind user overrides, expands synonyms before the 10-tag cap, and preserves forced R-18 / original tags.
- Civitai safety checks now match multi-word school/minor phrases from filenames and metadata instead of only exact split tokens.
- Pixiv and Civitai login flows now launch persistent Chrome without automation and sandbox default args, and the Pixiv account switch action immediately opens the login page.
- Web scheduler state is broadcast through SSE, restored on stream connection, and refreshed by the frontend when the next scheduled fire time passes.
- WD14 tagger setup copy now explains the haintag bridge, standalone tagger fallback, and model directory expectations more clearly.

### Notes
- `config.json` is still private runtime state and must not be committed.
- `pixiv/rule_fit/` contains generated rule-fit samples, manifests, and reports; keep only deliberate fixtures under version control.
