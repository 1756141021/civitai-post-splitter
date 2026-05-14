# civitai-post-splitter — 开发笔记

## 项目是什么

把 `upload/` 里的图片自动重发到 **Civitai / Pixiv / X (Twitter) / 小红书 (xhs)**。核心流程：

1. 从 `upload/` 选图
2. 读取图片 metadata（ComfyUI / A1111 prompt、LoRA token 等）
3. 用 WD14 tagger、metadata 实体提取和映射表构建 Pixiv 标题、说明、tag、年龄分级、原创/二创判断
4. 各平台按 `PLATFORM_RULES` 表决定是否走 sanitize / censor / LLM 反推
5. 按目标平台打开浏览器并提交（civitai/pixiv/x/xhs 各自独立 Playwright profile）
6. 写 manifest，成功后把图片移到 `done/`

入口：
- `launcher.py` — CLI 菜单入口
- `web_server.py` — Web UI 后端，端口 7788
- `civitai_splitter.py` — 核心命令；`cmd_upload` 是上传主流程

---

## 运行方式

- `run.bat` / `launcher.py`：本地菜单。
- `python civitai_splitter.py upload --targets civitai,pixiv --count 1`：直接上传。
- `python web_server.py`：启动 Web UI。

`config.json` 是本机私有运行配置，可能包含 API key 和 scheduler 状态，不提交。

---

## 主要文件

```
civitai_splitter.py       主命令入口，拆图 / 上传 / rule-fit 命令
web_server.py             Web UI 后端、SSE、任务队列、scheduler
launcher.py               CLI 菜单、账号切换、scheduler 配置
civitai_safety.json       Civitai 安全跳过规则
CHANGELOG.md              变更记录

frontend/
  mono-single.jsx         Web UI 源文件
  standalone.html         打包后的单文件 Web UI

x/
  support.py              X (Twitter) 发布：build_x_payload, create_x_post (Playwright)
  x_templates.json        模板（jp/en/zh × sfw/nsfw 6 套），每个含 core + social tag
  x_settings.json         tag_limit=2, default_template=en_sfw, 其他超时/重编码
  cookies.json            Cookie-Editor JSON 导入（auth_token + ct0）；.gitignore

xhs/
  support.py              小红书发布：build_xhs_payload, create_xhs_post (Playwright)
                          - 话题输入走 dropdown 选取（手打纯文本 # 不进算法）
                          - 自动勾 AI 合成声明 checkbox（GB45438-2025 强制）
  xhs_templates.json      默认模板（core=#AI绘画, social=#治愈系插画）
  xhs_settings.json       tag_limit=5, AI 标签 #AI创作 自动追加
  cookies.json            同 X，.gitignore

pixiv/
  support.py              Pixiv 核心函数：tag 构建、浏览器操作、rule-fit
  standalone.py           不依赖 haintag 的 metadata / WD14 后备实现
  danbooru_jp.json        151,262 条 EN→JP 主映射，来自 Pixiv 百科事典数据
  jp_aliases.json         人工覆盖映射，当前 2,402 条
  general_jp.json         Pixiv 通用配置：185 个 mappings、124 个 selling_points、6 组 synonym_tags
  tag_aliases.json        语义组、drop_tags、filename_drop_tokens
  tag_popularity.json     Pixiv live lookup / 直通 tag 计数缓存
  age_rules.json          文件名模式 → 年龄分级
  validation_cases.json   tag 映射回归用例，当前 200 条
  setup_censor.py         R-18 自动打码模型安装
  setup_tagger.py         WD14 tagger 配置向导
  rule_fit/               rule-fit 采样、manifest、报告运行产物，默认忽略

upload/                   待上传图片
done/                     上传成功后的图片
manifests/                每张图的上传记录
logs/                     失败截图和 HTML dump
```

---

## Pixiv tag 系统

Pixiv 需要日文 tag。WD14 / Danbooru 来源通常是英文 tag。转换分四层：

### 1. `danbooru_jp.json`

主映射表，151,262 条。来源是 HuggingFace `KaraKaraWitch/pixiv-dic-auto-translated`。

不要手改这里。需要更新就重新生成或重新下载。

### 2. `jp_aliases.json`

人工覆盖表。用于修正主映射翻错、漏词、角色名或作品名不理想的情况。

查找优先级：`general_jp.mappings` / `jp_aliases.json` 这类人工配置优先，然后才走大表和 live lookup。

### 3. `general_jp.json`

运行时可调的 Pixiv 规则表。

- `mappings`：普通 Danbooru tag 到 Pixiv 日文 tag。
- `synonym_tags`：命中 canonical tag 后追加别名，例如「ブルーアーカイブ」追加「ブルアカ」「BlueArchive」。
- `selling_points`：WD14 tagger 命中触发词和分数阈值后追加 Pixiv 高流量卖点 tag。
- `force_r18`：R-18 / R-18G tag 强制靠前，避免被 10 tag 上限截掉。
- `force_original`：原创图强制补「オリジナル」。

### 4. Pixiv live lookup / popularity cache

`build_pixiv_payload` 可以通过 Pixiv 页面做 live lookup 和 tag 计数，用结果更新 popularity 决策。它现在还会先从 metadata 里提取 Danbooru 风格的角色 / 作品实体（例如 `hatsune miku`、`name \(series\)`、已知 franchise tag），再和 WD14 结果一起排序。最终 tag 会按身份、作品/角色、卖点、tagger 分数和 Pixiv 计数排序，再压到 Pixiv 的 10 tag 上限。

---

## WD14 / haintag 集成

`HainTagBridge` 读取图片 metadata。`HainTagTaggerBridge` 调用 haintag 的 WD14 tagger。

配置在 `%APPDATA%/HainTag/settings.json`：

- `tagger_model_dir`：WD14 ONNX 模型目录。
- `tagger_python_path`：需要外部 Python 时使用。
- `tagger_local_enabled_categories`：默认 general / character / copyright。
- `tagger_local_general_threshold`、`tagger_local_character_threshold`：分类阈值。

如果 haintag 不存在或 tagger 不可用，上传不会中断，只会少一层 tag 候选。

---

## 平台规则表 (PLATFORM_RULES)

`civitai_splitter.py` 模块级常量。每个 target 声明四个属性：

| 平台 | needs_sanitize | needs_censor | needs_copy | max_age |
|------|:-:|:-:|:-:|:-:|
| civitai | ✗ | ✗ | ✗ | r18g |
| pixiv   | ✓ | ✓ | ✓ | r18g |
| x       | ✓ | ✓ | ✓ | r18g |
| xhs     | ✓ | ✓ | ✓ | **all_ages** |

- `needs_sanitize`: PIL re-encode 去除 EXIF / PNG text chunks（a1111 prompt 等）
- `needs_censor`: 跑 auto_censor 自动打码模型
- `needs_copy`: 消费 LLM 反推产出的标题/简介（驱动 `_targets_need_copy`）
- `max_age`: 该平台允许的最高 NSFW 等级。`xhs.max_age = all_ages` 意味着 r18/r18g 图自动从 targets 剔除并标 `skipped_max_age`，**用户即使在 UI 勾选也不发**

`needs_sanitize` / `needs_censor` 由 `any(rule for t in targets)` 聚合——只要有一个平台需要就跑，结果共享。`sanitized_artifact` 路径作为 X/xhs 的图源（继承打码）。

---

## manifest.copy 通用文案区

LLM 反推产出存进 `manifest.copy` 而非 platform-specific block，让 X/xhs/pixiv 都从同一处读：

```jsonc
"copy": {
  "title":   {"ja": "", "en": "", "zh": ""},
  "caption": {"ja": "", "en": "", "zh": ""},
  "llm_reverse": {
    "status": "ok|skipped_no_target_needs|skipped_nsfw_exceeds_account|disabled|failed",
    "persona_id": "...", "account_id": "...",
    "platform": "pixiv|x|xhs", "content_mode": "sfw|nsfw",
    "error": ""
  }
}
```

Pixiv 模块同时保留 `manifest.pixiv.title_ja` 等老字段作快照，向后兼容现有 pixiv-only 工具链。X/xhs 模块通过 `build_*_payload(..., copy=copy_block)` 参数读，缺则回落 pixiv 字段。

`apply_llm_result_to_copy_block(copy, result, platform, account_id)` 按 platform 把 LLM fields 映射到 copy 区：
- pixiv: `title_ja/zh + caption_ja/zh` → 对应 copy 字段
- x: `tweet` → `copy.caption.en`（X persona 默认产英文）
- xhs: `xhs_title + xhs_body` → `copy.title.zh + copy.caption.zh`

---

## LLM reverse

LLM reverse 是文案增强层。它只生成 `title_*`、`caption_*`，不接管 tag、年龄分级、censor、Civitai 过滤或发布动作。**按需触发**：targets 全是 civitai 时跳过整段。

配置保存在本地私有 `config.json.llm_reverse`：

- `base_url` / `api_key` / `model`：OpenAI 兼容视觉接口。
- `personas`：控制语言、标题风格、简介风格、SFW/NSFW prompt。
- `accounts`：绑定平台、默认 persona、默认 `content_mode`、允许的内容模式、**`max_nsfw_level`** (sfw/r18/r18g)。
- `content_mode=sfw`：给只能发 SFW 的平台或账号用。
- `content_mode=nsfw`：允许成人向文案；实际能否生成取决于接入的 LLM 服务。
- **NSFW 能力门禁**：图的 `age_restriction` 超过 account 的 `max_nsfw_level` → 跳过反推，写 `status=skipped_nsfw_exceeds_account`。避免让 sfw-only provider 处理 NSFW 图触发拒答或编造。

硬规则：LLM 文案不写政治、国家政治、政府、政党、意识形态、战争、领土争议、现实国家冲突等内容。命中时 manifest 记录 `political_blocked`，上传继续走原有 fallback。

Manifest 的 `pixiv.llm_reverse` 会记录：

- `status`: `ok` / `disabled` / `failed` / `political_blocked`
- `persona_id` / `account_id` / `content_mode`
- 生成的标题、简介、keywords
- 不含 API key 的错误摘要

验证方式：

```powershell
python -m py_compile civitai_splitter.py web_server.py pixiv/support.py pixiv/llm_reverse.py
python civitai_splitter.py upload --targets pixiv --count 1 --dry-run --llm-reverse --llm-persona pixiv_soft --llm-account pixiv_main --llm-content-mode sfw
python civitai_splitter.py upload --targets pixiv --count 1 --dry-run --llm-reverse --llm-persona pixiv_soft --llm-account pixiv_main --llm-content-mode nsfw
```

Web UI 入口：Settings → `LLM reverse` Configure；上传弹窗里勾选 `LLM 标题/简介`。

---

## Scheduler

Scheduler 有两套入口：

- Web UI Settings 区域写入 `config.json.scheduler`。
- launcher 菜单 `[9]` 可以配置并运行 CLI 调度循环。

配置字段：

```json
{
  "enabled": false,
  "targets": "civitai,pixiv",
  "count": 1,
  "min_hours": 1.0,
  "max_hours": 3.0,
  "next_fire_at": null
}
```

Web 后端 `_arm_scheduler` 会根据 `next_fire_at` 恢复倒计时。触发后调用上传任务，再写入下一次触发时间。前端通过 SSE 的 `scheduler_update` 实时刷新状态。

测试完要关掉 `enabled`，否则重启 Web UI 会继续恢复调度。

---

## Web UI 生命周期

Web UI 打开 `/api/stream` 建立 SSE 连接。页面关闭时前端会用 `navigator.sendBeacon('/api/shutdown')` 通知后端。

### 取消语义

- `web_server.py` 里的 worker 现在把 `InterruptedError` 统一收成任务 `canceled`，不再落成 `failed`。
- `launcher.py` 的更新检查通过 `cancel_event` 包装 git 子进程；取消发生在更新确认输入期间时，也不会误触发 pull。
- `cmd_upload` / `create_upload_manifest` / `create_civitai_post` / `create_pixiv_post` 现在只在“可逆阶段”响应取消。进入实际 publish 点击后，流程会优先完成收尾并保留成功结果，避免“远端已发成功、本地却显示 canceled”的假状态。


后端逻辑：

1. 有 SSE 客户端时取消 idle shutdown。
2. 页面关闭后，如果没有客户端，安排 idle shutdown。
3. shutdown 会先取消 scheduler。
4. 如果还有任务在跑，等任务空闲后再退出。

---

## 自动打码档位 (censor preset)

`pixiv/censor.json` 的 `preset` 字段控制 R-18 自动打码的覆盖范围：

| preset | enabled_classes | UI 显示 | 含义 |
|--------|-----------------|---------|------|
| `off` | `[]` | 关 | 不打码 |
| `japan` | `dick, vagina, anus, cum` | **Pixiv 标准** | Pixiv 平台合规线（生殖区域 + 体液，不含乳头） |
| `strict` | 加 `tits` | 严格 | 加乳头 |

Web UI Settings 区有下拉切换；切换走 `/api/censor-preset`，写入 censor.json 时同时改 `preset` 字段和 `enabled_classes` 列表。`cmd_upload` 读 `enabled_classes`（向后兼容），所以无需重启 Web 服务即可生效。

默认 `japan`（Pixiv 标准），对齐 X 算法对 NSFW 内容的"打码版进推荐流概率显著高于不打码"的实测（参见 CHANGELOG）。Preset ID `japan` 是历史名（最初按日本刑法 175 条命名），实际语义就是 Pixiv 平台合规线，UI 显示已校正。

---

## Civitai 安全跳过

`check_civitai_safety` 会先根据 `pixiv/age_rules.json` 推断年龄分级。只有命中 `civitai_safety.json.unsafe_ratings` 时才检查 minor / school tag。

检测来源包括文件名 token、metadata tag、以及多词短语。命中后跳过 Civitai，但 Pixiv 流程仍可继续。

---

## rule-fit 流程

rule-fit 是给 Pixiv tag 规则调参用的对照流程。

目录：`pixiv/rule_fit/`

- `samples/`：下载的 Pixiv 样图。
- `manifests/`：样图对应的 Pixiv tag / 流量 / 本地对比结果。
- `reports/`：汇总报告。

核心函数在 `pixiv/support.py`：

- `collect_rule_fit_sample_manifests`：从 ranking / hot tag 来源收集候选，按 bookmark、like、view、综合分挑样本。
- `download_pixiv_image_with_fallback`：优先下载 original，失败时回落 regular / large。
- `compare_rule_fit_samples`：用本地 tag 生成结果对比 Pixiv 原 tag。
- `summarize_rule_fit_report`：汇总 missing、extra、synonym mismatch、domain / age pattern。

`pixiv/rule_fit/` 是运行产物，默认不提交。需要固定样本时再单独挑选。

---

## 关键函数

| 函数 | 文件 | 说明 |
|------|------|------|
| `cmd_upload` | `civitai_splitter.py` | 上传主流程 |
| `create_upload_manifest` | `civitai_splitter.py` | 读取图片 metadata，构建 manifest |
| `check_civitai_safety` | `civitai_splitter.py` | Civitai minor / school 安全跳过 |
| `build_pixiv_payload` | `pixiv/support.py` | 构建 Pixiv tag、标题、说明、年龄分级 |
| `lookup_jp_alias` | `pixiv/support.py` | EN tag → JP tag 查找 |
| `create_pixiv_post` | `pixiv/support.py` | Playwright 操作 Pixiv 发布页 |
| `_arm_scheduler` | `web_server.py` | Web scheduler timer |
| `api_stream` | `web_server.py` | SSE 状态流 |
| `collect_rule_fit_sample_manifests` | `pixiv/support.py` | rule-fit 样本采集 |
| `compare_rule_fit_samples` | `pixiv/support.py` | rule-fit 本地/Pixiv tag 对比 |
| `_select_by_sort` | `civitai_splitter.py` | 按排序规则从 upload/ 取前 N 张 |
| `build_x_payload` | `x/support.py` | 构 X 推文 payload（tag/text/sensitive 标记） |
| `create_x_post` | `x/support.py` | Playwright 发 X 推（Ctrl+Enter 快捷键 + force-click fallback） |
| `pick_x_tags` | `x/support.py` | X tag 选择器（entity + template.core 或 template.core + template.social） |
| `build_xhs_payload` | `xhs/support.py` | 构小红书笔记 payload |
| `create_xhs_post` | `xhs/support.py` | Playwright 发小红书（话题走 dropdown，AI 声明 checkbox） |
| `apply_llm_result_to_copy_block` | `pixiv/llm_reverse.py` | LLM 反推 → manifest.copy 通用区 |
| `account_can_handle_age` | `pixiv/llm_reverse.py` | LLM account NSFW 能力 vs 图分级比对 |
| `_platform_accepts_age` | `civitai_splitter.py` | 平台 max_age 硬规则（小红书 NSFW 拦截）|
| `_targets_need_copy` | `civitai_splitter.py` | 任一 target 需要文案则触发 LLM |

---

## 图片选取排序

`cmd_upload` 支持 `--sort` 参数控制从 `upload/` 取图的方式：

| 值 | 行为 |
|----|------|
| `random`（默认） | `random.sample`，每次不同 |
| `name_asc` | 文件名 A→Z，取前 N 张 |
| `name_desc` | 文件名 Z→A，取前 N 张 |
| `time_asc` | 修改时间最旧优先，取前 N 张 |
| `time_desc` | 修改时间最新优先，取前 N 张 |

- `selected_names`（Web 传入的文件列表）传入时，顺序由调用方保证，`sort` 参数不参与选图，只在无文件列表时生效。
- 手动拖拽排序仅 Web UI 支持（CLI 文件多时不好操作）。Web 手动模式传 `sort=manual` + 有序 `files` 列表。
- Scheduler 的 `sort` 字段持久化到 `config.json`，默认 `random`。定时触发时沿用该排序规则。

---

## 登录状态

- Pixiv profile：`~/.civitai_splitter_pixiv_chrome`
- Civitai profile：`~/.civitai_splitter_chrome`
- X profile：`~/.civitai_splitter_x_chrome`
- 小红书 profile：`~/.civitai_splitter_xhs_chrome`
- Pixiv rule-fit profile：`~/.civitai_splitter_pixiv_rule_fit_chrome`

X 和小红书额外支持 `cookies.json` 导入：从普通 Chrome 用 Cookie-Editor 扩展导出 JSON 数组放在 `x/cookies.json` 或 `xhs/cookies.json`，启动时自动注入到 Playwright context（绕过 Google 登录被自动化浏览器拒的问题）。两个 cookies.json 都在 `.gitignore` 里——auth_token + ct0 等于账号完全访问权，绝不能 commit。

launcher 菜单 `[7]` 会清除 Pixiv profile 并立即打开登录页。`[8]` 同理处理 Civitai。

---

## 域名

- Pixiv 使用 `https://www.pixiv.net`。
- Civitai 登录和导航使用 `civitai.red`。
- Civitai 发布后的 URL 可能显示 `civitai.com`，这是 Civitai 自身行为。

---

## 常见坑

1. 不要手改 `danbooru_jp.json`。
2. 普通翻译补 `jp_aliases.json` 或 `general_jp.mappings`。
3. Pixiv 原生卖点才放 `selling_points`。
4. `config.json`、manifest、logs、rule-fit 样本都是本机运行状态，不要随手提交。
5. Web scheduler 测试完关掉 `enabled`。
6. Windows 路径和扩展名比较统一用 `.lower()` 或 `normcase()`。
