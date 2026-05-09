# civitai-post-splitter — 开发笔记

## 项目是什么

把 Civitai 帖子的图自动重发到 Civitai + Pixiv 双端。核心流程：

1. 从 `upload/` 随机抽图
2. 读取图片 metadata（ComfyUI / A1111 嵌入的 prompt、LoRA token 等）
3. 构建 Pixiv payload（日文标题、tag、年龄分级、原创声明）
4. 用 Playwright 浏览器自动登录并提交

入口：
- `launcher.py` — 交互菜单（CLI）
- `web_server.py` — Web UI，端口 7788
- `civitai_splitter.py` — 核心逻辑，`cmd_upload` 是上传主函数

---

## Tag 系统架构（最重要，看这里）

Pixiv 需要日文 tag。tagger 模型只输出 danbooru 英文 tag（如 `large_breasts`）。
整个 tag 系统负责把英文 → 日文，分三层：

### 第一层：danbooru_jp.json（主体，151,262 条）

**来源**：HuggingFace `KaraKaraWitch/pixiv-dic-auto-translated`
（Pixiv 百科事典机器翻译版，~15万词条，字段：`orig`=JP, `eng`=EN）

**作用**：EN danbooru tag → JP Pixiv tag 的权威映射表。
这是主查找源，覆盖了绝大多数常见 tag。

**不要手动往这里打补丁**。如果需要更新，重新从 HuggingFace 下载最新版。

### 第二层：jp_aliases.json（人工覆盖，2,149 条）

**作用**：覆盖 `danbooru_jp.json` 里翻译不准的词，或添加机翻漏掉的词。
格式：`{"EN_tag": "JP_tag"}`

查找顺序：`jp_aliases.json` 优先 > `danbooru_jp.json` > Pixiv ajax live lookup。

**只在 `danbooru_jp.json` 翻译错误或缺失时在这里补**。

### 第三层：selling_points（规则推断，在 general_jp.json）

**作用**：处理那些没有一对一英文对应的 Pixiv 原生 JP tag。
例如「極上の女体」——没有一个英文 danbooru tag 叫这个，但 tagger 检测到 `nude` 时应该追加它。

格式：
```json
{"trigger": ["nude", "naked"], "tag": "極上の女体", "min_score": 0.6}
```

**只用于真正需要推断的 Pixiv 原生 tag**，不要把 danbooru_jp.json 已有的映射重复放到这里。

### tag_aliases.json（语义组）

控制哪些 EN tag 会被语义归组（例如 `twintails` / `twin_tails` 归为同一组），
以及 `drop_tags`（上传时排除）、`filename_drop_tokens`（文件名解析时排除的噪声词）。

### tag_popularity.json

记录同一概念有多个竞争 JP tag 时的胜者（如「眼鏡」vs「メガネ」）。
通过 Pixiv live lookup 实时更新投票计数。

---

## 文件目录速查

```
civitai_splitter.py       主逻辑（cmd_split / cmd_upload）
web_server.py             Web UI 后端，端口 7788
launcher.py               CLI 菜单入口

pixiv/
  support.py              Pixiv 所有核心函数（tag 构建、浏览器操作）
  danbooru_jp.json        ★ 151K 条 EN→JP 主映射（来自 Pixiv 百科事典）
  jp_aliases.json         人工覆盖 2149 条
  general_jp.json         运行时配置（selling_points / mappings / force_r18 等）
                          （首次运行从 support.py DEFAULT_GENERAL_JP 生成）
  tag_aliases.json        语义组 / drop_tags / filename_drop_tokens
  tag_popularity.json     竞争 JP tag 的 live 投票结果
  age_rules.json          文件名模式 → 年龄分级规则
  validation_cases.json   tag 映射回归测试用例（6K+条）
  setup_censor.py         R-18 自动打码模型安装
  setup_tagger.py         WD14 tagger 安装（cl_tagger）

upload/                   待上传图片放这里
done/                     上传成功后移到这里
manifests/                每张图的上传记录 JSON
logs/                     失败截图和 HTML dump
```

---

## 关键函数

| 函数 | 文件 | 说明 |
|------|------|------|
| `cmd_upload` | civitai_splitter.py:~600 | 上传主流程 |
| `create_upload_manifest` | civitai_splitter.py:~419 | 读取图片 metadata，构建 manifest |
| `build_pixiv_payload` | pixiv/support.py:~1318 | 三层 tag 查找，生成 Pixiv 提交 payload |
| `lookup_jp_alias` | pixiv/support.py:~1255 | 单个 EN tag → JP tag 查找（三层顺序） |
| `create_pixiv_post` | pixiv/support.py:~1871 | Playwright 浏览器操作，实际提交 |
| `_arm_scheduler` | web_server.py | 定时自动上传 timer |

---

## Pixiv 登录

- Profile 目录：`~/.civitai_splitter_pixiv_chrome`
- 登录状态检测：`PIXIV_PROFILE_DIR.exists()`
- 清除登录：Web UI `[Switch account]` 按钮 / launcher 菜单 [7]
- 重新登录：下次上传时自动弹出浏览器

Civitai 登录同理，profile 在 `~/.civitai_splitter_chrome`，launcher 菜单 [8] 清除+重开。

---

## 定时上传（Scheduler）

配置在 `config.json` 的 `scheduler` key。
Web UI Settings 区域可配置间隔（min/max hours）、张数、目标平台。
`next_fire_at` 是 ISO 时间戳，服务器重启后恢复倒计时。

---

## 域名

- Civitai 登录/导航用 `civitai.red`（镜像，绕过访问限制）
- 发布后 post URL 仍会是 `civitai.com`（Civitai 自身行为，正常）

---

## 常见坑

1. **不要改 danbooru_jp.json**，有问题改 jp_aliases.json 或 selling_points
2. **selling_points 只用于 Pixiv 原生 tag**，不要把普通映射塞进去
3. `general_jp.json` 不存在时从代码里的 `DEFAULT_GENERAL_JP` 自动生成，删掉它可以重置
4. Scheduler 测试完记得把 `enabled` 改回 false，不然重启服务器会立即开始上传
5. `config.json` 里有 API key，不要提交到 git
