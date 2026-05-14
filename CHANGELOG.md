# Changelog

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
