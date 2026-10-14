# 源码讲解 · 第 13 章：创建技能（Creating Skills）

> 维护说明：章节交付时的统计与实测输出保留原口径；当前发布前修复、套件数字与验证收据见《发布前修复记录.md》。维护节说明当前契约；正文与附录仍含各阶段源码片段，按段内提交读取。

> 上游：`官方文档-Developer-Guide/creating-skills.md`（438 行）· 中文精译见 `中文精译-13-CreatingSkills.md`（684 行）。
> 真实侧行号锚 `f5d1926110`（v0.21.3）· mini 侧混合了初始实现与后续维护阶段，按段内提交及具体引文定位；章节交付快照为 `02c61d5`。
> ★ **引用归属约定**：mini 侧引用一律写成 `mini-hermes/<路径>:<行号>`；**未带 `mini-hermes/` 前缀的裸 `文件:行号` 一律指真实侧**（本章 mini 与真实侧有 12 个同名同路径文件，不加前缀无法判归属）。

> 术语说明：secret 指密钥、令牌等机密值，并不表示它已加密；dotpath 指用点分隔的配置路径，如 `skills.config.myplugin.path`；tap 指用户登记的技能源仓库。

[TOC]

---

## 导言 · 机制总览：Hermes 具体怎么做的（先读这一节）

第 9 章的外部集成协议在 mini 中未实现；第 12 章以地图和没有生产者的后台回执为主。第 11 章则已实现状态库恢复，不能归为“几乎无实现项”。本章相反：**mini 的提示词装配层自己就写着这个接缝** —— `agent/system_prompt.py` 的模块 docstring 逐字：「Hermes 的 volatile 层**以"技能索引"打头**（理由：索引未变时可保留在记忆块之前的公共前缀中）。**第 13 章把它补上了**」（`:32-33`），而 `:303` 那行 `volatile_parts` 列表头就是那个位置（`:306` 的 `*_skill_index_parts(agent)` 接在首位）。commit `02c61d5` 就是"把它补上"：**发现 + 渐进披露 + 一个 `skill_view`**，三件，不碰 `run_agent.py` / `model_tools.py`。

真实 Hermes 的技能系统是一条长链：发现 → 解析 frontmatter → 渲染索引进系统提示词 → 模型按需 `skill_view` 读正文；外围还挂着安装 / 安全扫描 / 发布 / 蓝图 / 策展器。下面七小节先把**主干那条链**（发现 + 渐进披露 + `skill_view`）按「谁在管 / 什么时候 / 做什么 / 什么不能破 / 出错怎么办 / 跨了哪几层 / 一次加载的时间线」拆开；每条落到真实源码的 `文件:行号` 与**逐字引文**（锚 `f5d1926110`，v0.21.3）。外围那一圈只进 §〇 总表，不进本节的链。

### 0.1 谁在管（哪个模块 / 哪个函数是入口）

管这件事的是「**一份发现实现 + 两个消费者**」：

| 角色 | 入口 |
|---|---|
| 发现（"去哪找 + 优先级"） | `agent/skill_utils.py::get_all_skills_dirs`（`:398`，`local → create_dir → external`）+ 项目层 `get_project_skills_dirs`（`:409-413` 注释）+ 全局序 `get_scan_ordered_skills_dirs`（`:802-811`，`project → local → external`，**首见胜出**） |
| 扫描（"怎么遍历"） | `agent/skill_utils.py::iter_skill_index_files`（`:761-783`，**排序后**才 yield） |
| 消费者①：系统提示词索引 | `agent/system_prompt.py::_skills_prompt`（`:300`，无 skills 工具则返回 `""`）→ `agent/prompt_builder.py::_render_skills_index`（`:1327`） |
| 消费者②：按需读正文 | `tools/skills_tool.py::skills_list`（`:232`）/ `skill_view`（`:571`） |

逐字引（`agent/skill_utils.py:409-413`）：

```text
# Project-local skills (<root>/.hermes/skills, <root>/.agents/skills; root = nearest
# .git ancestor) are a prompt-injection vector if auto-sourced from any clone, so
# they load only when the root is in ``skills.trusted_project_dirs``; then they
# override same-named profile/bundled skills. cwd + trust list are session-fixed
# so the skills index stays byte-stable.
```

**要害**：这段注释把"谁在管"钉成两句 —— ①**项目技能默认不在发现范围**（`cwd + trust list` 是**会话固定项**，要显式 trust 才加载，否则它就是 clone 注入面）；②末句直接给出整条理由链的终点：**发现范围必须会话固定，索引才字节稳定**。所以"谁在管"不是某一个模块，而是"**发现给出固定顺序 + 索引渲染 + 工具读取**"三处**共用同一份扫描**（`get_scan_ordered_skills_dirs` 是唯一排序源）。

### 0.2 什么时候（索引生成 / 正文读取分别发生在哪一刻）

两个时点，切得干净：**索引**在每次重建系统提示词时生成；**正文**只在 `skill_view` 被调用时读取。

- 索引：`agent/system_prompt.py:708` 的 `volatile_parts` 首项就是 `skills_prompt`；真实侧另有**磁盘快照**快路径（`agent/prompt_builder.py::_skills_prompt_snapshot_path` `:1110-1112`）与**进程内 LRU**（`_SKILLS_PROMPT_CACHE_MAX = 32`，`:1106`）。
- `skills.auto_load` 钉住的技能**只在首个 build 解析一次**，此后进 stable prefix（注释 `agent/system_prompt.py:684-685`；`_auto_load_parts` `:316-320` docstring）。
- 正文：`skill_view` 调用点（模型主动 load / 用户 `/skill-name` / auto_load 钉住）；真实侧把"**查看**"也算作"**使用**"（`tools/skills_tool.py:704-712`）。

逐字引（`agent/system_prompt.py:705-707`）：

```text
# ── Volatile tier (most likely to differ on a rebuild; kept last so the stable prefix stays reusable) ──
# Skills are runtime-mutable, so the index leads the volatile band: on a longest-prefix
# backend an unchanged index stays inside the reused prefix; a changed one re-prefills from here.
```

**要害**：源码把运行期可变的技能索引放在 volatile 层，并让它先于记忆块。索引不变、后面的记忆块变化时，索引仍能留在公共前缀中；索引变化时，从索引起重新预填充。不能由此断言它是“本层最不易变的块”，也不能把“变化点放最前”解释成缓存损失最小；源码没有提供变化频率的实测。mini `_skill_index_parts()` 采用同样的排序。

### 0.3 做什么（索引渲染 + 正文读取的完整动作）

"渲染索引"只做一件事：**取 name + description，description 硬截 60**，每行 `- {name}: {desc}`。

- 字段抽取 `agent/skill_utils.py::extract_skill_description`（`:750`）；
- 上限常量 `SKILL_PROMPT_DESC_LIMIT = 60`（`:741`）；
- 骨架 `agent/prompt_builder.py::_render_skills_index`（`:1352-1376`）：`## Skills` + 加载指令 + `<available_skills>` + 每行 `    - {name}: {desc}` + `</available_skills>`。

"读正文"做的事：`skill_view` 返回**全文**（**不分页**，硬规矩见 `skills/AGENTS.md:57`）+ `linked_files`（第二跳的入口）。

逐字引（`agent/skill_utils.py:750-753`）：

```text
def extract_skill_description(frontmatter: Dict[str, Any]) -> str:
    """Extract a system-prompt-length description from parsed frontmatter."""
    desc = _normalize_skill_description(frontmatter)
    return desc[:SKILL_PROMPT_DESC_LIMIT - 3] + "..." if len(desc) > SKILL_PROMPT_DESC_LIMIT else desc
```

**要害**：`[:LIMIT - 3] + "..."` 是**含省略号一共 60 字符**，不是"60 字符再加省略号"。这是作者软规范（`skills/AGENTS.md:28-31`「**`description` ≤ 60 chars**」）在渲染层的**硬兜底** —— 一条没守规矩的描述不至于把索引（每轮请求都在上下文里）撑爆。

### 0.4 不变式（什么不能破）

两条：

1. **索引字节稳定**：扫描结果**排序后**才 yield（`agent/skill_utils.py:761-783` 的 `yield from map(Path, sorted(matches))`），磁盘快照用 manifest 逐文件签名比对（`agent/prompt_builder.py:1110-1125`）—— 同一批技能两次构建必须字节相同。
2. **索引不自动携带正文**：普通发现只放 name + desc；显式 `auto_load` 可把正文放进 stable 层，`skill_view` 则按需返回正文。

逐字引（`tools/skills_tool.py:2-5`，模块 docstring）：

```text
"""Skills Tool — list and view skill documents (progressive disclosure). A skill is a directory
holding SKILL.md (YAML frontmatter + instructions) plus optional references/, templates/, assets/,
scripts/. `skills_list` returns name/description only; `skill_view` returns full content and
linked files. Sibling modules (skills_tool_setup / _plugin / _dedup) re-export here."""
```

**要害**：这一句就是本章的**核心承诺**——`skills_list`（索引那一跳）**只回 name/description**，`skill_view`（第二跳）才回全文。所以"索引不携带正文"不是纪律口号，是**两个工具各自的返回契约**：索引工具手里根本没有正文字段可回。

### 0.5 出错（三类失败，三档处置）

- **技能文件坏** → **宽容**：frontmatter YAML 解析失败**回落** `key: value` 逐行切分；`name` 缺失**回落目录名**（`tools/skills_tool.py:604`）—— 坏技能仍能进索引。
- **平台 / 条件未命中** → **软隐藏**：只是"不进索引那一行"，不删文件、不拒绝加载。
- **`skills.disabled`** → **硬拒绝**：`skill_view` 直接 `_fail(...)`；且 `hermes-agent` **永不可禁用**（`agent/skill_utils.py:273-275`）。

逐字引（`tools/skills_tool.py:604-606`）：

```text
        resolved_name = frontmatter.get("name", skill_md.parent.name)
        if _is_skill_disabled(resolved_name):
            return _fail(f"Skill '{resolved_name}' is disabled. Enable it with `hermes skills` or inspect the files directly on disk.")
```

**要害**：两件事同框 —— ①`name` 缺失**回落目录名**是宽容（坏 frontmatter 也救得回来）；②`disabled` 是**硬拒绝**（不是"隐藏那一行"，是 load 直接失败）。把"**条件未命中（软隐藏）**"与"**disabled（硬拒绝）**"区分开，是这一节最容易写错的一句。

### 0.6 跨层（与提示词装配 / 工具 / 配置的关系）

跨五层：`system_prompt`（**排序**：索引进 volatile 首块）· `prompt_builder`（**内容构造**）· `skill_utils`（**发现 / 解析**）· `tools/skills_tool`（**按需读**）· config（`disabled` / `auto_load` / `template_vars`）。其中最容易被漏掉的一层是**支持目录**：`references/` 里的大文档**是渐进披露的第二跳，不是独立技能**。

逐字引（`agent/skill_utils.py:29-31`）：

```text
# Progressive-disclosure support dirs inside a skill package: loaded explicitly
# via skill_view(skill, file_path=...), never scanned as standalone skills.
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))
```

**要害**：`references/` 里的文档**不被扫成独立技能**（`iter_skill_index_files` 里 `has_skill_md and d in SKILL_SUPPORT_DIRS` 就剪掉），它只在**第二次调用** `skill_view(name, file_path=...)` 时出现 —— 这正是"跨层"的实质：**渐进披露的第一跳在工作区（提示词），第二跳在工具（按需）**。

### 0.7 时间线（从"磁盘上一个 SKILL.md"到"模型真的用上它"）

1. `SKILL.md` 躺在 `~/.hermes/skills/<cat>/<name>/`（或 external / 已 trust 的项目目录）。
2. 重建提示词时 `_skills_prompt(agent)` 调 `build_skills_system_prompt(...)`（`agent/system_prompt.py:300`）；**没有 skills 工具则整块 `""`**。
3. 若命中磁盘快照就跳过全扫，否则 `iter_skill_index_files` 遍历 + `parse_frontmatter` 抽字段，渲染成 `- {name}: {desc}` 组进 `<available_skills>`。
4. 索引块进 **volatile 层首项**（`agent/system_prompt.py:708`）。
5. 模型读提示词看到 `- alpha: …` ⇒ 主动调 `skill_view(name="alpha")`。
6. `skill_view` 解析名字 → 读正文 → 返回**全文 + `linked_files`**；同时 `bump_view` / `bump_use`。
7. 需要附文件时**第二次**调 `skill_view(name, file_path="references/x.md")`。

逐字引（`agent/system_prompt.py:301-302`）：

```text
    """Skills index (empty without skills tools).  Focus mode demotes non-coding
    categories to names-only — never hidden, every name stays visible."""
```

**要害**：这句钉死两个门 —— ①"**empty without skills tools**"：没有 skills 工具就**没有索引块**（索引与工具同生共死，mini 的 `has_skills()` 就是这个门的单一事实源）；②focus 模式只把分类降成"只有名字"，**永不隐藏**（对应渲染层的 `NEVER drop entries`）。第 5 步是整条链的**交接点**：提示词只让模型**知道存在什么**，普通按需路径的正文留在下一跳；`auto_load` 是显式提前加载的另一条路径。

### 验收四问

1. **索引里会不会顺手把正文也带进提示词？** 不会。只放 name + desc；`Skill` dataclass **没有正文字段**（`agent/skill_utils.py:90-101`），`build_skill_index` 连正文都不加载。（§0.3 · §0.4）
2. **技能索引为什么在 volatile 层、又为什么排最前？** 当前排序把索引放在 volatile 层的记忆块之前；索引未变时可随公共前缀复用，变化时从这里重算（`agent/system_prompt.py:705-707`）。（§0.2）
3. **`skill_view` 拿一个名字怎么定位文件？会不会被拼路径越界？** 关键判据是"名字是**寻址键**，不是路径片段"——真实侧对名字走路径校验，mini 的形状更强：名字只做**相等比较**，路径永远来自扫描结果（见 §〇 表 · §0.5）。
4. **技能库为空时会怎样？** 索引是**空串**（`build_skill_index` 无技能返回 `""`），`_join_tier` 把空项整个丢掉 ⇒ 提示词里既没有标题也没有多余空行（字节确定）；`skill_view` 的 `check_fn` **同时**收起工具。（§0.7）

---

## §〇 与真实版的差距总表

★ **本章在 mini 侧实现了技能主干。** 第 11 章已实现状态库恢复，第 12 章的后台回执缺生产者；本章 mini 侧在 commit `02c61d5` 落地了技能系统的**最小形状**（发现 + 渐进披露 + 一个 `skill_view`）——因为它自己的 docstring 就写明了这个接缝（`agent/system_prompt.py:32-33`「原来还差一块……第 13 章把它补上了」，`:303` 的 `volatile_parts` 就是那个位置）。下表按「面 | 真实 Hermes | mini 现状」逐面拉平；mini 列四档口径：`✅ 已抄（02c61d5）` / `已对齐` / `⬜ 没做（量级）` / `⏭ 不抄（理由）`。真实侧锚 `f5d1926110`（v0.21.3），初始实现快照 `02c61d5`；内置根扩展 `4b0c96a`，当前行为以维护代码为准。

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 技能目录 + 发现 | 四层优先级 `local → create_dir → external`（`agent/skill_utils.py:398-406`）+ 项目层单独、**默认不加载、要显式 trust**（`:409-413`、`:802-811`，首见胜出） | ✅ 已抄（02c61d5）：用户根与内置根均只扫**一层** `skills/<name>/SKILL.md`（`agent/skill_utils.py:121-152`），每 profile 一套（走 `get_mini_home()`）。窄在**没有"分类"这个消费者**（无 browse / 无 hub 安装），等真有需求再补递归 |
| **渐进披露**（索引只 name+desc，正文经 `skill_view`）★ | 索引渲染 `agent/prompt_builder.py:1327-1376`；正文 `tools/skills_tool.py:571` | ✅ 已抄（02c61d5）：内容构造 `load_skill_index()`（`agent/prompt_builder.py:645`）+ `build_skill_index`（`agent/skill_utils.py:372-400`）+ `skill_view`（`mini-hermes/tools/skills.py:84-124`）。**真跑收据**：`demos/ch13_real_run.py` 17/17 通过，含"正文独有句在提示词里搜不到" |
| 描述硬截 60 字符 | `SKILL_PROMPT_DESC_LIMIT = 60`（`agent/skill_utils.py:741`）+ `:750-753` | ✅ 已抄（02c61d5）：`agent/skill_utils.py:69`、`:360-369`（含省略号一共 60） |
| 索引进 volatile 层**首块**（复用前缀缓存）★ | `volatile_parts = [skills_prompt, ...]`（`agent/system_prompt.py:705-708`） | ✅ 已抄（02c61d5）：`_skill_index_parts()` 接进 `volatile_parts` **首位**（`agent/system_prompt.py:195-213`、`:303-310`） |
| 扫描结果**排序**（索引字节稳定） | `yield from map(Path, sorted(matches))`（`agent/skill_utils.py:761-783`） | ✅ 已抄（02c61d5）：`discover_skills` 结尾 `skills.sort(key=name)`（`agent/skill_utils.py:315`）；真跑"连续两次构建字节完全一致" |
| 快照 + 进程内 LRU（32 条） | `_SKILLS_PROMPT_CACHE` + `~/.hermes/.skills_prompt_snapshot.json`（`agent/prompt_builder.py:1106-1125`、`:1110-1112`） | ⬜ 没做（`discover_skills` 每次**真读盘**，无缓存；量级是"几个文件 × 每轮重建"，真实那套是给 ~100 技能 × 每轮 —— 等真慢了再补） |
| `skills.auto_load` 钉 stable prefix | `_auto_load_parts`（`agent/system_prompt.py:316-320`、`:684-685`） | ⬜ 没做（量级 ~12 行，需先有 `skills.auto_load` 配置消费面） |
| `skill_view` 工具（只读、**不分页**、第二跳） | `tools/skills_tool.py:571` + `:705-716` 注册 | ✅ 已抄（02c61d5）：`mini-hermes/tools/skills.py:84`，走**普通注册路径**（只读文件 ⇒ 不进 agent loop，`mini-hermes/tools/skills.py:13-19` 判据） |
| `skills_list`（Tier 1 列表工具） | `tools/skills_tool.py:232` | ⏭ 不抄（mini 的"列表"就是提示词索引本身；再开一个列表工具与索引重复，且多一份漂移面） |
| frontmatter 解析 | 真 YAML，失败回落逐行切分（`agent/skill_utils.py:105-125`） | 已对齐：零依赖**子集**解析器（只认顶层 `key: value`；嵌套 / 列表 / 行尾注释一律忽略）（`mini-hermes/agent/skill_utils.py:256-344`）。窄在"只为 name/description 两个标量"，为两个字段引 YAML 解析器不值（硬约束：零第三方依赖） |
| `platforms:` 宿主 OS 门 | `PLATFORM_MAP` + `skill_matches_platform`（`agent/skill_utils.py:21`、`:128-145`），含 Termux 特判 | ⬜ 没做（mini 没有按宿主 OS 过滤技能的能力（已有 CLI 和 console 网关）；`agent.platform` 那个是界面平台 cli/tui，不是 OS） |
| 条件激活 `_CONDITION_KEYS`（含未文档化的 `session_platforms`） | 五键定义 `agent/skill_utils.py:633-641` + 判定 `agent/prompt_builder.py:1210-1228` | ⬜ 没做（可映射 mini 的 toolset 概念，**若将来扩展，可先接 `requires_toolsets` / `fallback_for_toolsets` 两键**，实现规模需实际评估；`session_platforms` 不抄——mini 未接技能渠道过滤） |
| `environments:` 第三道**相关性**门 | `agent/skill_utils.py:148-153`（fail open、显式加载永远绕过、只影响 offer 面） | ⏭ 不抄（mini 无 kanban / docker / s6 环境面） |
| `skills.disabled` 硬拒绝 + `hermes-agent` 不可禁 | `tools/skills_tool.py:605-606`；`ESSENTIAL_SKILLS`（`agent/skill_utils.py:273-275`） | ⬜ 没做（mini 有 `config.json`，~10-15 行可补；只有配上前面的 config 才有意义） |
| Secure Setup on Load（三别名合一 + 网关降级 + 密钥值不进模型上下文） | `tools/skills_tool_setup.py:46-63`、`:87-118` | ⏭ 不抄（mini 无技能密钥征询与落盘流程；作用域是只读快照） |
| 环境变量自动 passthrough 到 sandbox | `tools/skills_tool.py:438-442` | ⏭ 不抄（mini 无 sandbox 后端 / passthrough 注册表） |
| 凭据文件挂载（主库永不可挂、fail-closed） | `tools/credential_files.py:79-104`；Docker `:ro`（`tools/environments/docker.py:440-462`） | ⏭ 不抄（mini 无 Docker / Modal / SSH 远端后端） |
| `skills.config` + `config migrate`（"Skill Settings"） | `agent/skill_utils.py:692-693`；`hermes_cli/config.py:993-1011`、`:1520-1548` | ⏭ 不抄（mini 的 config 是**无迁移的平铺 JSON**，没有版本迁移面） |
| 安全扫描 + 四档信任 + `--force` | `tools/skills_guard.py:24-33`、`:660-673` | ⏭ 不抄（mini 未实现来源分级或内容扫描；内置根随程序发布，用户根可手写或复制第三方文件） |
| 发布 / 安装 / tap / 三种发现模型 | `hermes_cli/skills_hub.py`、`tools/skills_hub_*.py` | ⏭ 不抄（mini **零依赖**、无技能分发客户端与安装事务；已有模型 HTTP 调用与网页搜索，但没有技能下载、安装事务） |
| 蓝图（skill 声明 `metadata.hermes.blueprint`） | `tools/blueprints.py:1-7`、`:44-62` | ⏭ 不抄（mini 无 cron / 调度面） |
| 建议的定时任务四来源 + consent-first | `cron/suggestions.py:1-9`、`:37`（`MAX_PENDING = 5`） | ⏭ 不抄（mini 无编排面；第 12 章刚确认没有生产者） |
| curator（闲置触发、只碰 `created_by: agent`、只归档不删） | `agent/curator.py:1-6`、`:29-34`；`tools/skill_usage.py:1-6` | ⏭ 不抄（mini 有常驻网关，但没有技能策展器与对应的闲置触发） |
| 语义检索 / catalog | 嵌入模型或远端检索 | ⏭ 不抄（mini 只有 FTS5，且服务于**消息**；更窄的替代 = **名字 / 描述子串匹配**，成本约几十行） |

**两条准确性备注**

- 第 2 行与第 4 行是这个机制的**一对判据**，常被拆开讲错：**索引只放 name+desc** 解决的是"上下文别被正文撑爆"，**索引进 volatile 首块**解决的是"改了技能别废掉缓存前缀"——前者是内容约定，后者是**排序约定**，两件事。
- 第 12 行的"只抄两键"不是妥协而是**对齐 mini 的语义**：真实 `_CONDITION_KEYS` 五键里，`requires_toolsets` / `fallback_for_toolsets` 命中的正是 mini 已有的 toolset 概念（`toolsets.py:53` 那张表），另外三键（`requires_tools` / `fallback_for_tools` / `session_platforms`）要么依赖"可用工具集"的运行时快照、要么依赖网关渠道门，mini 已有工具可用性快照和 console 网关，但尚未把这些条件接入技能过滤。

---

## §一 该做成技能还是工具？

**① Hermes 怎么做的**

这是一条**判据**，不是一个机制——原文用两个清单给出边界：**做成技能**，当能力可以表达成**指令 + shell 命令 + 现有工具**、包装的是一个 agent 能用 `terminal` / `web_extract` 调用的外部 CLI 或 API、**不需要把定制 Python 集成或 API 密钥管理烤进 agent**（例：arXiv 搜索、git 工作流、Docker 管理、PDF 处理）；**做成工具**，当需要与 API 密钥 / 鉴权流程 / 多组件配置做端到端集成、需要每次都必须精确执行的定制处理逻辑、或处理二进制 / 流式 / 实时事件（例：浏览器自动化、TTS、视觉分析）。

判据的分界线其实是**"这份能力是数据还是代码"**：技能是**目录 + 一份 Markdown 说明书**（agent 读了会照做，能力靠已有工具组合出来），工具是**注册进 registry 的 Python**（每次执行都由代码保证同样的行为）。所以"要不要烤进 agent"这句话，落到代码上就是"要不要新增一个 `@register_tool`"。

**② 读 Hermes 的代码**

逐字引（`agent/skill_utils.py:762-765`，`iter_skill_index_files` docstring）：

```text
    """Walk skills_dir yielding sorted paths matching *filename*; prunes
    EXCLUDED_SKILL_DIRS and support dirs of skill roots. Org mirrors are
    TOKEN-GATED: only the active org's subdir is walked, so leaving an org
    stops its skills resolving without manual cleanup."""
```

**要害**：一个技能在系统里就是"**磁盘上一个 SKILL.md 文件**"，系统**走目录去发现它**（`Walk skills_dir`）——而一个工具是注册表里一条带 handler 的记录，系统**查注册表就能拿到它**。这份 docstring 体现的正是"技能 = 数据"：没有 import、没有函数，只有被遍历到的文件。判据的代码侧落点就在这里。

**③ mini 的实现（现状）**

mini 没有技能**创作**流程（没有 `skill_manage`），所以 §一 的判据在 mini 里只能作为**纪律**存在，不产生代码。但这条判据**已经用在 mini 自己的一个决策上**：`skill_view` 该做成"普通注册工具"还是"agent 级工具（像 `todo_list` 那样）"。判据不是"它看起来简不简单"，而是**执行它需不需要 agent 对象**：

```text
# tools/skills.py:13-19
`todo_list` 要（状态挂在 agent 上，执行由 run_agent.INLINE_TOOL_EXECUTORS 接管）；
`skill_view` 只读文件、不写任何状态，所以它就是普通注册工具 ——
@register_tool + toolsets.py 点名，不用碰 run_agent.py / model_tools.py。
```

结果：`skill_view` 只需 **2 个文件**（新建 `tools/skills.py` + 改 `mini-hermes/toolsets.py:74/99`），AST 自动发现会认它（`mini-hermes/model_tools.py:112`）。这正是"数据 vs 代码"判据在 mini 里的直接产物——只读文件的能力天生归"技能 / 数据"侧，连工具实现都因此更轻。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 判据本身 | 文档级（`creating-skills.md:11-23`），无强制代码 | 已对齐：同一条判据，用在 `skill_view` 的注册路径选择上（`mini-hermes/tools/skills.py:13-19`） |
| 技能 = 数据 | 走目录发现磁盘上的 `SKILL.md`（`agent/skill_utils.py:761`） | ✅ 已抄（02c61d5）：`agent/skill_utils.py` 把技能当"目录 + frontmatter"读，无 import、无函数 |

---

## §二 技能目录结构

**① Hermes 怎么做的**

技能住在两个**表面**里：`skills/`（随包内置、**默认加载**、按类别组织）与 `optional-skills/`（重依赖 / 小众，随仓库发出但**不激活**，要用 `hermes skills install official/<category>/<skill>` 装）。两者**同一套结构**：`<surface>/<category>/<skill-name>/SKILL.md`（必需）+ 可选的 `scripts/`（辅助脚本）、`references/`（大文档）、`templates/`、`assets/`。

关键字不在"目录怎么摆"，而在**"支持目录是第二跳，不是独立技能"**：`scripts/`、`references/`、`templates/`、`assets/` 里的文件**不参与技能发现**（扫描时被剪掉），只在 `skill_view(name, file_path=...)` 的第二次调用里出现。所以一个技能目录里"住着什么"分两类：**一个是入口（SKILL.md），其余都是按需打开的抽屉**。

**② 读 Hermes 的代码**

逐字引（`skills/AGENTS.md:6-12`）：

```text
## Two surfaces

- **`skills/`** — built-in, loadable by default, organised by category (`skills/github/`, `skills/mlops/`).
- **`optional-skills/`** — heavier/niche skills shipped but NOT active; installed via
  `hermes skills install official/<category>/<skill>` (adapter `tools/skills_hub_official.py`
  `OptionalSkillSource`). Categories: `autonomous-ai-agents, blockchain, communication, creative,
  devops, email, health, mcp, migration, mlops, productivity, research, security, web-development`.
```

**要害**：两个表面**同一结构、不同激活策略** —— `skills/` 默认加载，`optional-skills/` 靠 `official` 这个 provenance 值拿到 builtin 信任级、但**必须显式 install 才激活**。这条分界的判据是**受众**（真的对大多数用户普遍有用 ⇒ 内置），不是技术难度。

**③ mini 的实现（现状）**

mini 落**两个来源根、一层深度**：**用户根** `<MINI_HOME>/skills/<name>/SKILL.md`（`mini-hermes/agent/skill_utils.py:144-154` 的 `skills_dir()` = `get_mini_home() / "skills"`）+ **内置根** `<repo>/mini-hermes/skills/<name>/SKILL.md`（`:156-169` 的 `bundled_skills_dir()`，随程序发布）。两者的先后**就是优先级**（`:172` 的 `skill_roots()` = 用户在前 ⇒ 同名时**用户覆盖内置**，首见胜出）—— 注意这与真实侧"表面"的含义**不同**：真实侧 `skills/` vs `optional-skills/` 分的是**激活策略**（默认加载 / 显式 install），mini 没有那一层的消费者，所以没抄；第 13b（`4b0c96a`）补的是**来源**面的第二级。深度仍只有一层，没有 `<category>/`（`mini-hermes/agent/skill_utils.py:75-80` 的 `SUPPORT_DIRS` 常量**留着但不参与扫描**；"为什么一层就够"写在 `:29-31` 的 docstring 里）。

"每个 profile 一套技能"这件事**没另写一行 profile 逻辑**：路径走 `get_mini_home()`（`mini-hermes/hermes_constants.py:36`，解析顺序 contextvar 覆盖 → `MINI_HOME` → `~/.mini-hermes`），而"一个 profile = 一个 home"，所以隔离是**天然**的。同时 `mini-hermes/hermes_cli/profiles.py:43` 把 `skills` 加进 `PROFILE_DIRS`，于是**新建 profile 那一刻就建好技能库**（而不是等用户自己 `mkdir`——那会是一条隐式约定）。

注意一处**刻意不照抄**：真实侧会递归走分类目录（`skills/github/xxx`），mini 只扫一层。判据是"**有没有分类这个消费者**"——mini 没有 browse、没有 hub 安装，分类目录无处可用，所以不做投机性实现。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 表面数（激活策略） | 两级：`skills/`（默认加载）+ `optional-skills/`（显式 install）（`skills/AGENTS.md:6-12`） | ⬜ 没做（`optional-skills/` 的消费者是 hub 安装面，mini 没有）。★ 第 13b（`4b0c96a`）补的是**来源**面的第二级（内置根 + 用户根、用户优先），不是这一行说的"激活策略" |
| 目录深度 | `<surface>/<category>/<skill>/SKILL.md`，扫描**递归**（`agent/skill_utils.py:761-783`） | 已对齐（窄）：`<MINI_HOME>/skills/<skill>/SKILL.md`，只扫**一层**（`agent/skill_utils.py:133-152`） |
| 支持目录 | `references/templates/assets/scripts` 被剪、走第二跳（`agent/skill_utils.py:29-31`） | 已对齐：常量在（`:81-86`），扫描剪枝逻辑同形（一层扫描下无嵌套可剪） |
| "技能属于谁" | profile = HERMES_HOME，靠目录隔离 | ✅ 已抄（02c61d5）：走 `get_mini_home()`；`PROFILE_DIRS` 加 `skills`（`hermes_cli/profiles.py:43`） |

---

## §三 SKILL.md 格式

`SKILL.md` = **YAML frontmatter**（首部元数据，`---` 围栏）+ Markdown 正文。frontmatter 声明能力的全部"身份与依赖"，正文写"怎么用"。

```markdown
---
name: my-skill
description: Brief description (shown in skill search results)
version: 1.0.0
author: Your Name
license: MIT
platforms: [macos, linux]          # 宿主 OS 门（顶层字段）
metadata:
  hermes:
    tags: [Category, Subcategory, Keywords]
    related_skills: [other-skill-name]
    requires_toolsets: [web]               # 条件激活（仅在这些工具集活跃时显示）
    requires_tools: [web_search]
    fallback_for_toolsets: [browser]       # 主工具在场时隐藏
    fallback_for_tools: [browser_navigate]
    config:                                # 非敏感设置 → skills.config.<key>
      - key: my.setting
        description: "What this setting controls"
    blueprint:                             # 声明后即是"可运行的自动化"
      schedule: "0 9 * * *"
required_environment_variables:            # 环境变量需求
  - name: MY_API_KEY
    prompt: "Enter your API key"
    help: "Get one at https://example.com"
    required_for: "API access"
---
```

**① Hermes 怎么做的**

frontmatter 用**真 YAML** 解析，解析失败**回落** `key: value` 逐行切分（坏格式不致命）；首行的 UTF-8 BOM **先剥掉**，否则首行 `---` 对不上围栏检查。字段是三组：**身份**（`name` / `description` / `version` / `author` / `license`）、**门**（`platforms:` 顶层，宿主 OS）、**`metadata.hermes.*`**（`tags` / `category` / `related_skills` / 四个条件激活键 / `config` / `blueprint`）。顶层 `tags:` / `category:` 被 loader **镜像**进 `metadata.hermes.*`。

正文侧有一条硬规矩：**技能不被分页**（`skills/AGENTS.md:57`：`No offset/limit pagination on skill-loading tools — the agent must read a skill fully (root)`）——因为"instructional 工具"分了页，模型就拿**半份说明书**干活。

**② 读 Hermes 的代码**

逐字引（`agent/skill_utils.py:105-108`，`parse_frontmatter` docstring）：

```text
def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from markdown; returns (frontmatter_dict, body).
    Malformed YAML falls back to key:value line splitting. A leading UTF-8 BOM
    (Windows editors) is stripped first or it would defeat the ``---`` fence check."""
```

**要害**：三句话是三个真实坑 —— ①**返回值是二元组**（frontmatter 字典 + 正文），正文要能原样交出；②**坏 YAML 回落逐行切分**（宽容，不是拒绝）；③**BOM 必须先剥**，否则 Windows 编辑器存的文件首行 `---` 匹配不上，整个 frontmatter 都被当成正文。

**③ mini 的实现（现状）**

mini 写了一个**零第三方依赖的子集解析器**（`mini-hermes/agent/skill_utils.py:256-344`），**不是 YAML**：只认顶层 `key: value`，嵌套（`metadata.hermes.*`）、列表（`- a`）、多行块、行尾注释**一律忽略**。判据是"**索引只需要 `name` / `description` 两个标量**"——条件激活 / 蓝图 / `skills.config` 这些嵌套字段的消费者都不在 A 批，为两个标量引一个 YAML 解析器不值（项目硬约束：零第三方依赖）。真要加嵌套的那天，改动面就是这一个函数 + 一条测试。

两处与真实侧**同形**：①**先剥 BOM**（`mini-hermes/agent/skill_utils.py:280-281`，注释点明"真实侧同样先剥 BOM —— 否则首行 `---` 对不上"）；②**围栏未闭合宽容地交回全文**（`:288`），对应"坏格式不致命"。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| frontmatter 解析 | 真 YAML + 逐行回落（`agent/skill_utils.py:105-125`） | 已对齐（窄）：零依赖子集解析器（`mini-hermes/agent/skill_utils.py:256-344`） |
| 字段集 | 身份 + `platforms:` + `metadata.hermes.*`（含四条件键 / `config` / `blueprint`）（`skills/AGENTS.md:16-22`） | ⬜ 只消费 `name` / `description` 两个标量（其余字段对 mini 无消费者） |
| 正文分页 | 明确**不分页**（`skills/AGENTS.md:57`） | ✅ 已抄（02c61d5）：`skill_view` 一次返回全文（`mini-hermes/tools/skills.py:84-124`） |

### 平台专属技能（Platform-Specific Skills）

**① Hermes 怎么做的**

用**顶层** `platforms:` 字段把技能限定到特定 OS：`[macos]` / `[macos, linux]` / `[windows]`。值是**人类可读的 OS 名**，判定时经一张映射表换成 `sys.platform` 前缀。设了之后，在不兼容的平台上，该技能**自动从系统提示词、`skills_list()`、斜杠命令里隐藏**（软隐藏）；**省略或为空 ⇒ 所有平台都加载**（向后兼容）。特别注意：`platforms:` 在**顶层**，与条件激活里的 `session_platforms`（在 `metadata.hermes.*`、指网关渠道）**不是一回事**，层级也不同。

**② 读 Hermes 的代码**

逐字引（`agent/skill_utils.py:136-138`）：

```text
        # Termux is a Linux userland on Android: accept linux-tagged skills
        # whether sys.platform is "linux" (pre-3.13) or "android" (3.13+).
        if sys.platform.startswith(mapped) or (running_in_termux and mapped in ("linux", "termux", "android")):
```

**要害**：`platforms:` 不是**字符串相等**判定，而是"**换成 sys.platform 前缀再前缀匹配**"（`PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}`，`:19`）——所以 `macos` 能匹配 `darwin`。而 Termux 特判暴露了一个真实缺口：Android 上的 Linux 用户态在 Python 3.13 后 `sys.platform` 从 `"linux"` 变成 `"android"`，一个 `linux` 标签的技能差点因此在 Android 上失效。

**③ mini 的实现（现状）**

⬜ 没做。mini 没有按宿主 OS 过滤技能的能力（已有 CLI 和 console 网关）——`agent.platform`（`mini-hermes/run_agent.py:256`，默认 `"cli"`）指的是**界面平台**（cli / tui），不是宿主 OS，与 `platforms:` 语义无关。mini 里能"按条件开关一段东西"的现成机制是 `check_fn` 或 config 段开关，但两者都不需要 OS 判定，因此当前未接这条门；跨 OS 使用技能时仍可能需要它。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| `platforms:` 宿主 OS 门 | `PLATFORM_MAP` + `skill_matches_platform`（`agent/skill_utils.py:21`、`:128-145`），含 Termux 特判 | ⬜ 没做（mini 未实现技能的宿主 OS 过滤） |
| 不匹配的处置 | 从提示词 / `skills_list` / 斜杠命令**软隐藏**（不删文件） | 无此项（同一"软隐藏"语义由 §3.2 的条件激活承担，而条件激活也没做） |

### 条件激活（Conditional Skill Activation）

**① Hermes 怎么做的**

技能可以声明对特定工具 / 工具集的依赖，**控制它是否出现在某会话的系统提示词里**：

| 字段 | 行为 |
|---|---|
| `requires_toolsets` | 所列工具集中**任一个**不可用 ⇒ **隐藏** |
| `requires_tools` | 所列工具中**任一个**不可用 ⇒ **隐藏** |
| `fallback_for_toolsets` | 所列工具集中**任一个**可用 ⇒ **隐藏** |
| `fallback_for_tools` | 所列工具中**任一个**可用 ⇒ **隐藏** |

两个用例：`fallback_for_*` 造"主工具不在时的替代方案"（如 `duckduckgo-search` 带 `fallback_for_tools: [web_search]`，只在 web 搜索没配好时出现）；`requires_*` 造"只有某些工具在场才说得通"的工作流。**关键判据**：这四键只影响"**是否进索引那一行**"——未命中**不等于技能被移除**（不删文件、不拒绝加载），真正的加载拒绝是 `skills.disabled`（见 §0.5）。此外代码里还有**第五键** `session_platforms`（网关渠道门，文档未列），它取的是**当前渠道**（`HERMES_PLATFORM` / `HERMES_SESSION_PLATFORM`），与宿主 OS 的 `platforms:` 完全不同。

**② 读 Hermes 的代码**

逐字引（`agent/skill_utils.py:633-635`）：

```text
# ``session_platforms`` is the gateway-channel gate: session platforms the skill
# is FOR (hidden from the index elsewhere), unlike ``platforms:`` (host OS).
_CONDITION_KEYS = ("fallback_for_toolsets", "requires_toolsets", "fallback_for_tools", "requires_tools", "session_platforms")
```

**要害**：`_CONDITION_KEYS` 五键**全部从 `metadata.hermes.*` 取**（`extract_skill_conditions` 只读 `_hermes_metadata(frontmatter)`，`:638-641`）——所以 `session_platforms` 写在**顶层**会**静默失效**。注释还直接点明它与 `platforms:` 的区别：一个是**网关渠道**门（"技能只发给这些渠道"），一个是**宿主 OS** 门。判定逻辑在 `agent/prompt_builder.py:1210-1228` 的 `_skill_should_show`：`fallback_for` 命中即隐、`requires` 缺一即隐，且**网关渠道门先判、与工具信息无关，平台未知时 fail open**。

**③ mini 的实现（现状）**

⬜ 没做。但 mini **有可映射的承载物**：它已经有一张 toolset 表（`mini-hermes/toolsets.py:53` 的 `TOOLSETS`）与 `check_fn` 探测机制（`mini-hermes/tools/registry.py:180-186`）。所以这条门若要抄，**若将来扩展，可先接 `requires_toolsets` / `fallback_for_toolsets` 两键**即可——它们命中的正是 mini 已有的"工具集是否可用"概念；另外三键（`requires_tools` / `fallback_for_tools` / `session_platforms`）要么依赖"可用工具集"的运行时快照，要么依赖网关渠道门，mini 已有工具可用性快照和 console 网关，但尚未把这些条件接入技能过滤。量级约 20~30 行。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 条件激活键 | 五键（四键文档化 + `session_platforms`）（`agent/skill_utils.py:633-641`） | ⬜ 没做（`requires_toolsets` / `fallback_for_toolsets` 两键可映射到 mini 的 toolset） |
| 判定函数 | `_skill_should_show`（`agent/prompt_builder.py:1210-1228`），未命中 ⇒ 不进索引、**不删技能** | 无此项（同一位置由"空技能库 ⇒ 索引空串 + 工具收起"承担） |
| 渠道门 `session_platforms` | 网关渠道闸门（`metadata.hermes.session_platforms`） | ⏭ 不抄（mini 未接技能渠道过滤概念） |

### 环境变量需求（Environment Variable Requirements）

**① Hermes 怎么做的**

技能用 `required_environment_variables` 声明自己需要的环境变量（每项：`name` 必需，`prompt` / `help` / `required_for` 可选）。当技能经 `skill_view` **加载**时，它声明的必需变量会被**自动登记**，以便**透传进沙箱化的执行环境**（`terminal`、`execute_code`）——包括 Docker / Modal 这类远端后端。**关键判据**：**缺值不隐藏技能**（技能照常可发现、可加载），只标记；而透传只登记**实际已设置**的变量。用户也可在 `config.yaml` 的 `terminal.env_passthrough` 里手工配置透传。

**② 读 Hermes 的代码**

逐字引（`tools/skills_tool.py:438-441`）：

```text
    # Only vars actually set pass through to sandboxed execution (execute_code, terminal).
    if available_env_names := [e["name"] for e in required_env_vars if e["name"] not in remaining]:
        try:
            from tools.env_passthrough import register_env_passthrough
            register_env_passthrough(available_env_names)
```

**要害**：登记的判据是"**实际已设置**"（`e["name"] not in remaining`，`remaining` 是仍缺的那些）——一个**声明了但没配**的变量**不会**被登记透传，所以沙箱里它不存在。这避免了"技能以为变量在、实际拿到 `None`"的静默失败：技能的声明是**需求**，透传是**已满足的需求**，两者分开。

**③ mini 的实现（现状）**

⏭ 不抄。迷你**没有沙箱后端**（`terminal` 就是本机 shell，无 Docker / Modal），也**没有 `env_passthrough` 注册表**——沙箱透传这条链在 mini 里没有落点。更窄的替代：mini 若要给技能传配置，走的是既有 `config.json`（`hermes_cli/config.py` 的三段式读法）；若真要环境变量，直接靠宿主进程环境即可，无需"声明 + 登记 + 透传"三段。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 环境变量声明 | `required_environment_variables`（+ `setup.collect_secrets` + 旧别名 `prerequisites.env_vars` 三别名合一）（`tools/skills_tool_setup.py:46-63`） | ⏭ 不抄（mini 无密钥面 / 无三别名兼容包袱） |
| 加载时自动透传 | 只登记"实际已设置"的变量进沙箱（`tools/skills_tool.py:438-442`） | ⏭ 不抄（mini 无 sandbox / passthrough 注册表） |
| 缺值处置 | **不隐藏技能**，只标 `setup_needed`（`tools/skills_tool_setup.py:19-22`） | 无此项（无 setup 流程） |

## §四 加载时的安全设置（Secure Setup on Load）

**① Hermes 怎么做的**：技能可以声明自己需要 API 密钥或令牌（`required_environment_variables`）。这里有一条**与直觉相反的纪律**：缺值**不会**把技能从"可发现"里藏起来——技能照样进系统提示词索引、照样能被 `skill_view` 加载；缺的只是"万事俱备"这个状态，由 `setup_needed` / `readiness_status` 标出来。

声明面是**三别名合一**：`required_environment_variables`、`setup.collect_secrets`、以及向后兼容的旧别名 `prerequisites.env_vars`，去重校验成一张表（首个出现者胜出）；变量名必须匹配 `_ENV_VAR_NAME_RE`（`^[A-Za-z_][A-Za-z0-9_]*$`），非法直接丢。真正加载时（`skill_view` / 斜杠命令 / `skills.auto_load`）走一条**就绪检查**：先看哪些必需变量**既不在 `.env` 快照、也不在密钥作用域**里，缺的才去征询。

征询按**界面**分叉，这是本节最关键的一处边界：网关/消息面**没法安全弹框**，就返回一段 `gateway_setup_hint`，让用户去本地 CLI 或手填 `~/.hermes/.env`；只有桌面/TUI 带上交互标志（`HERMES_INTERACTIVE`）时才走安全回调。而这条回调的**返回值**是整章安全观感最强的地方——值写进 `.env` 由回调另一侧完成，回给调用方的只有 `success / stored_as / validated / skipped` 四个状态词，**原始机密值从不流经模型上下文**。

加载成功后还有一步"**自动透传**"：只有**实际已设置**的那些变量会被 `register_env_passthrough` 登记，好让 `execute_code` / `terminal` 沙箱（含 Docker、Modal 远端后端）里能读到 `$TENOR_API_KEY`；而 Hermes 自己的 provider 凭据被硬性拦下——技能不得覆盖沙箱的凭据清洗（GHSA-rhgp-j443-p4rf）。若声明的是**文件形态**的凭据（`required_credential_files`），则改走另一条路（见下 H3）。

**② 读 Hermes 的代码**：`tools/skills_tool_setup.py:104-117`（密钥捕获回调的消费段，逐字）：

```text
104:    if (callback := _st._secret_capture_callback) is None:
105:        return _capture_result(missing_names)
106:    remaining_names: List[str] = []
107:    for entry in missing_entries:
108:        metadata = {"skill_name": skill_name, **{k: entry[k] for k in ("help", "required_for") if entry.get(k)}}
109:        try:
110:            callback_result = callback(entry["name"], entry["prompt"], metadata)
111:        except Exception:
112:            logger.warning(f"Secret capture callback failed for {entry['name']}", exc_info=True)
113:            callback_result = {"success": False, "stored_as": entry["name"], "validated": False, "skipped": True}
114:        ok = isinstance(callback_result, dict) and callback_result.get("success")
115:        if not (ok and not callback_result.get("skipped")):
116:            remaining_names.append(entry["name"])
117:    return _capture_result(remaining_names, bool(remaining_names))
```

要害：整段代码对回调返回值**只问两件事**——`success`（成不成，`:114`）与 `skipped`（跳没跳，`:115`）；`stored_as` 里装的是**变量名**（`:113`），不是值；函数最后交出去的也只有"还剩哪几个没配"（`:117`）。**值本身没有任何一条路径能回到模型上下文**——它由回调写进 `~/.hermes/.env`，这边只收状态词。这就是文档那句"永不把原始机密值暴露给模型"的代码出处。

**③ mini 的实现（现状）**：⏭ **不抄——缺的正是安全面的两头**。

- **缺 secret 的写入面**：mini 的 `agent/secret_scope.py` 只有**只读**接口——`get_secret(key)`（`:108`）在多路复用模式外回落 `os.environ`、在模式内无作用域则抛 `UnscopedSecretError`（`:117、:130`）。它确有 `set_secret_scope(dict)`（`:79`），但那是**进程内把已拿到的密钥装进 contextvar**（`:24-25` 的 ContextVar），不是"向用户安全征询一个值再落盘"的写面。没有回调注册点（真实侧 `_st._secret_capture_callback`），也就没有"值不经模型"这条契约要守——这仅说明缺少安全征询流程；只读凭据、工具输出仍有各自的保密边界。
- **缺容器后端**：透传的目标（`execute_code`/`terminal` 沙箱、Docker、Modal）在 mini 里不存在（`tools/terminal.py` 是同步本机 `subprocess.run`）。所以"把已设置变量透传进沙箱"在 mini 里没有目的地。

**推理链**：就绪状态机（`available/setup_needed/unsupported`）的存在意义 = "缺依赖时**标记而非隐藏**，并把征询安全地做掉"。mini 既没有"缺依赖"这一状态要表达（未解析技能声明的外部依赖），也没有安全征询的写面——**照抄一个状态机会是一个永远返回 `available` 的状态机**，正是本项目反复拒绝的投机性基础设施。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 缺密钥是否隐藏技能 | **不隐藏**：`setup_needed` / `readiness_status` 只标记（`tools/skills_tool.py:456-462`） | ⏭ 不抄（无加载期就绪状态机） |
| 密钥征询 | 安全回调只回 `success/stored_as/validated/skipped`，值写 `.env`（`tools/skills_tool_setup.py:104-117`） | ⏭ 不抄：`agent/secret_scope.py:108` 只有只读 `get_secret`，无写入面 |
| 沙箱透传 | 只登记**已设置**的变量（`tools/skills_tool.py:437-442`），provider 凭据被拦（`tools/env_passthrough.py:56-60`） | ⏭ 不抄（无沙箱/远端后端） |

### 配置项设置（Config Settings (config.yaml)）

**① Hermes 怎么做的**：与机密值分流的是**非敏感**设置——路径、偏好、其它非敏感值。声明写在 frontmatter 的 `metadata.hermes.config`（每项 `key` 必需 / `description` 必需 / `default`、`prompt` 可选），缺 `key` 或 `description` 的项直接跳过；存储前缀是常量 `SKILL_CONFIG_PREFIX = "skills.config"`，值落进 `config.yaml` 的 `skills.config.<logical key>`。

**发现**与**注入**是两个面：`hermes config migrate` 扫**所有已启用且平台兼容**的技能、按 key 去重、把未配置的项提示给用户（`Configure skill settings? [y/N]:`）；`hermes config show` 里则单独有一段 `_section("Skill Settings")`。**运行时注入**发生在技能加载时——解析出的值被拼进技能消息，形如 `[Skill config (from <HERMES_HOME>/config.yaml): …]`，于是 agent 看到已配置的值，**不必自己去读 `config.yaml`**。

这里有一个极易读错的坑：配置值里的 `~` / `$HOME` 是**按"工具子进程的 HOME"展开**的，不是主进程 HOME。

**② 读 Hermes 的代码**：`agent/skill_utils.py:709-715`（路径展开，逐字注释）：

```text
709: def _expand_skill_config_path(value: str) -> str:
710:     """Expand ``~`` / ``$HOME`` against the HOME Hermes injects into tool subprocesses.
711:
712:     Skill config defaults describe paths the agent hands to tools, so in a container where the
713:     control process HOME (``/opt/data``) differs from the tool HOME (``{HERMES_HOME}/home``) a
714:     plain ``expanduser`` pointed the prompt at a path no tool would ever read (#12260).
715:     """
```

要害：docstring 把理由逐字写清了——配置默认值描述的是"**agent 交给工具去读的路径**"，所以展开必须对齐**工具子进程的 HOME**（`get_subprocess_home()`，`:716`）；直接 `expanduser` 会按控制进程 HOME（容器里可能是 `/opt/data`）展开，指向一个**没有任何工具会去读**的路径（#12260）。这条不显眼，却是"配置值真的可用"与"配置值看着对但没人读"的分水岭。

**③ mini 的实现（现状）**：⏭ **不抄——缺配置版本迁移面**。mini 的配置是**无迁移的平铺 JSON**（`hermes_cli/config.py` 三段式：`load_config` → `_section` → `get_<段>_config`，默认值在该层夹取），没有 `config.yaml` 那样的命名空间、没有 migrate 交互、也没有"按技能声明发现未配置项"这一步。`agent/skill_utils.py:12、:173` 的 docstring 把 `skills.config` 明确列在"没抄"清单里，理由正是"无配置迁移面"。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 存储 | `skills.config.<key>`（`agent/skill_utils.py:692-693`；抽取 `:644-666`） | ⏭ 不抄（配置为平铺 `config.json`，无命名空间） |
| 发现 / 征询 | `hermes config migrate`（`hermes_cli/config.py:1519-1524`，`Configure skill settings? [y/N]:`） | ⏭ 不抄（无迁移机制） |
| `config show` 的 "Skill Settings" 段 | `hermes_cli/config.py:2971-2983`（`_section("Skill Settings")`） | ⏭ 不抄（无段展示） |
| 运行时注入块 | `[Skill config (from …/config.yaml): …]`（`agent/skill_commands.py:190-196`） | ⏭ 不抄 |

### 凭据文件需求（Credential File Requirements (OAuth tokens, etc.)）

**① Hermes 怎么做的**：以**文件**形态（而非环境变量）存放的凭据——典型是安装脚本产出的 OAuth 令牌文件——用 `required_credential_files` 声明（每项 `path` 必需、相对 `~/.hermes/`，`description` 可选）。加载时检查文件是否存在：**缺失 → `setup_needed`**；存在 → 自动处理，但处理方式**按后端分三种**：Docker 里以**只读绑定挂载**挂进去；Modal 里**同步**（创建时 + 每条命令之前，故会话进行中的 OAuth 也能刷新）；本地后端无需特殊处理。

这里的关键不是"怎么挂"，而是**挂载面的信任边界**，有三层：① 只收 `HERMES_HOME` 相对路径，绝对路径与 `..` 拒绝；② **主凭据库永不可挂**——即使它就住在 `HERMES_HOME` 内、因此过了包含性检查，也照样拒绝（走 canonical 读拒绝清单，同一个 `agent.file_safety.get_read_block_error`）；③ 这条拒绝是 **fail-closed**——守卫拿不到就宁可拒挂。Docker 那一侧再叠一道：绑定参数一律带 `:ro`。

**② 读 Hermes 的代码**：`tools/credential_files.py:80-84`（逐字注释）：

```text
80:    # Master credential stores are never mountable, even though they sit inside HERMES_HOME and therefore
81:    # pass the containment check above. Fails CLOSED: if the canonical guard can't be consulted we refuse
82:    # the mount rather than risk bind-mounting auth.json into a sandbox. The import lives at module top (no
83:    # circular-import concern — file_safety is stdlib-only); the sentinel + logger.exception keep guard
84:    # failures debuggable instead of silently swallowed (#67665).
```

要害：**容器边界不能比读边界更松**——文档说"技能可以挂它自己的服务令牌"，实现把上限钉死在"**永远挂不上主密钥文件**"（注释逐字 *a skill may mount its own service token, not the master key files*，`:96-97`）；而且这条拒绝是 fail-closed：守卫 import 失败（`:85-88`）或抛异常（`:91-93`）都返回拒挂。同一个读拒绝清单同时服务"读"和"挂"两个面，于是"在挂载面递给技能的东西"不会超过"在读面允许它读的东西"。

**③ mini 的实现（现状）**：⏭ **不抄——缺容器后端，也缺凭据写面**。凭据文件机制的全部意义是"把宿主文件安全地送进**远端沙箱**"，mini 没有 Docker/Modal/SSH 这些后端（终端是本机同步 `subprocess.run`），所以既无处可挂、也无"沙箱初始一个文件都没有"这个问题要答；而 `required_credential_files` 所依赖的"凭据被写出来"这一步，又和上一节缺的 secret 写面同源。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 缺失 → `setup_needed` | `tools/skills_tool.py:447-453`（`setup_needed = setup_needed or bool(missing_cred_files)`） | ⏭ 不抄（无就绪状态机） |
| Docker 只读挂载 | `-v host:container:ro`（`tools/environments/docker.py:462`；理由见注释 `:443-447`） | ⏭ 不抄（无容器后端） |
| Modal 创建时+每条命令前同步 | `tools/credential_files.py:1-5` 模块 docstring | ⏭ 不抄 |
| 主凭据库永不可挂、fail-closed | `tools/credential_files.py:80-98` | ⏭ 不抄（无挂载面） |

---

## §五 技能编写准则（Skill Guidelines）

### 不引入外部依赖（No External Dependencies）

**① Hermes 怎么做的**：优先 stdlib Python、`curl` 以及**现有的 Hermes 工具**（`web_extract`、`terminal`、`read_file`）；真需要一个依赖时，把安装步骤写进技能里。这条纪律在 `skills/AGENTS.md` 里被抬成**硬性规范**：技能正文点名原生工具（反引号包裹），**绝不点名 agent 已经包过的 shell 工具**——`grep` 要写成 `search_files`、`cat`/`head`/`tail` 写成 `read_file`、`sed`/`awk` 写成 `patch`；第三方 CLI 只允许出现在脚本文件里，不作为榜首出口。

**② 读 Hermes 的代码**：`skills/AGENTS.md:32-36`（作者规范第 2 条，逐字）：

```text
32: 2. **Prose references native Hermes tools or the MCP servers the skill expects, in backticks**
33:    (`terminal`, `web_extract`, `read_file`, `patch`, `search_files`, `vision_analyze`,
34:    `browser_navigate`, `delegate_task`). Never name shell utilities the agent has wrapped: `grep` →
35:    `search_files`, `cat`/`head`/`tail` → `read_file`, `sed`/`awk` → `patch`, `find`/`ls` →
36:    `search_files target='files'`. MCP dependencies are named with setup in `## Prerequisites`.
```

要害：规范不是"别用 grep"，而是"**别把 agent 已经包好的 shell 工具当成榜首出口**"——技能正文该点名 Hermes 原生工具，把 `grep→search_files` 这类映射**逐条写死**（`:34-36`），第三方 CLI 只降格到脚本文件（`:37`）。这条同时消掉了"模型按旧习惯敲 `grep`"这条漂移路径。

**③ mini 的实现（现状）**：✅ **已对齐（纪律层，无代码）**。mini 的硬约束就是**零第三方依赖**——这条在 `mini-hermes/agent/skill_utils.py:275` 的 docstring 里被点名（*为了两个标量字段引入一个 YAML 解析器不值（本项目硬约束：零第三方依赖）*），并在 `model_tools.py` / `toolsets.py` / `profiles.py` 的"等真有需求再补"里一致体现。同一份纪律也让 mini 的 frontmatter 解析器只做**子集**（`mini-hermes/agent/skill_utils.py:256-344`：只认顶层 `key: value`，拒绝嵌套/列表/多行块）。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 依赖纪律 | stdlib / `curl` / 原生工具；依赖须写安装步骤（`skills/AGENTS.md:32-36`） | ✅ 已对齐（纪律层）：零第三方依赖是硬约束（`agent/skill_utils.py:174`） |

### 渐进披露（Progressive Disclosure）

**① Hermes 怎么做的**：这是**本章的核心**。整套技能系统对上下文只暴露**两跳**：第一跳是系统提示词里的一行**紧凑索引**——每个技能一行 `    - 名字: 描述`，描述被**硬截到 60 字符**；第二跳是模型按需调 `skill_view(name)` 拿**正文**。普通索引路径不把正文自动塞进系统提示词；上游显式 `auto_load` 是例外。

为什么必须这样：① **token 经济**——技能库里几十上百个技能，正文全塞进提示词会当场炸掉上下文，而绝大多数技能当轮用不到；② **缓存不变式**——索引被放在 **volatile 层的首块**，让"没变的那一行留在被复用的前缀里、变了才从这一行重新 prefill"，这是当前采用的排序；它保住变化点之前的公共前缀，不能据此证明这是唯一可行的排序。

还有一条配套的**硬规矩**：技能加载类工具**没有 `offset`/`limit` 分页**——模型必须一次读完技能全文（分了页它会只读第一页就跳过其余，拿着半份说明书干活）。这是"数据工具必须分页、instructional 工具绝不"这条更一般纪律的一个实例。

**② 读 Hermes 的代码**：`agent/skill_utils.py:741` 与 `:750-753`（索引描述的取值，逐字）：

```text
741: SKILL_PROMPT_DESC_LIMIT = 60
...
750: def extract_skill_description(frontmatter: Dict[str, Any]) -> str:
751:     """Extract a system-prompt-length description from parsed frontmatter."""
752:     desc = _normalize_skill_description(frontmatter)
753:     return desc[:SKILL_PROMPT_DESC_LIMIT - 3] + "..." if len(desc) > SKILL_PROMPT_DESC_LIMIT else desc
```

要害：函数名逐字是 `extract_**skill_description**`，docstring 逐字写明它取的是"**系统提示词长度**的描述"；而 `desc[:60 - 3] + "..."` 说明省略号**算在 60 之内**——不是"60 字符再加省略号"。这是索引那行唯一允许出现的东西，和作者规范"`description` ≤ 60 字符"（`skills/AGENTS.md:28`）互为兜底：作者守规矩时原样保留，作者写长了也不会撑爆索引。而索引在提示词里的最终形状由 `agent/prompt_builder.py:1356` 决定——`index_lines.append(f"    - {name}: {desc}" …)`，**只有名字与描述，没有路径、版本、正文**。

**③ mini 的实现（现状）**：✅ **真的做了（02c61d5）**，而且是本章唯一落地的核心机制。

- **发现 + 解析**：`agent/skill_utils.py`（409 行）是本机制的**唯一实现**——`discover_skills()`（`:288-317`）扫 `<MINI_HOME>/skills/*/SKILL.md`、只取 `name`/`description`、坏技能**跳过并计数**；`Skill` 数据类（`:90-101`）里**根本没有正文字段**（docstring 逐字：*少一个字段 = 少一条"正文不小心进提示词"的路*）。
- **索引渲染**：`build_skill_index()`（`:372-400`）只碰 frontmatter，输出 `## 可用技能（技能索引）` + 每行 `- 名字: 描述`；`truncate_description()`（`:360-369`）照真实算法 `desc[:60-3] + "..."`（注释点明"含省略号一共 60 字符"）；**没有技能 → 返回空串**（`:391-392`），不是"空块"（空块会往 volatile 层插换行 = 另一种字节差异）。
- **进提示词**：`mini-hermes/agent/prompt_builder.py:645` `load_skill_index()`（薄封装，保证扫描只有一份实现）被 `mini-hermes/agent/system_prompt.py:195` 的 `_skill_index_parts` 取用，插在 `volatile_parts` **列表头**（`:303-306`，在 `_memory_parts` 之前）。
- **第二跳**：`tools/skills.py`（136 行）的 `skill_view` 走**普通注册路径**（不需要 agent 对象，故不碰 `run_agent.py`/`model_tools.py`），返回正文 + 目录列表，**不分页**（schema 只有 `name` 一个参数）。

**真跑收据**（`demos/ch13_real_run.py`，17/17 通过）：① 真提示词连续两次构建**字节完全一致**、`invalidate` 之后重建仍一致；② 索引块就是 volatile 层开头；③ **★ 正文独有句（"正文独有句：鹦鹉会在凌晨三点背诵圆周率。"）不在真提示词里**，`p1.count("鹦鹉") == 0`；④ 同一句话只有 `skill_view("alpha")` 能取出，且取回的正文仍不在提示词里；⑤ 描述确实被硬截到"`前缀 + ...`"共 60 字符；⑥ 无技能库时索引是**空串**（提示词里连标题都不出现，`skill_view` 工具也同时不可见）。配套契约测试 `tests/test_skills.py` **72/72（6 组）**，其中组 1 钉"索引字节稳定"（写无关文件、落一条消息、只改 mtime 都不变）、组 2 钉"**普通发现不自动加载正文**"。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 索引内容 | 每行 `    - {name}: {desc}`（`agent/prompt_builder.py:1356`） | ✅ 已抄（02c61d5）：`- {name}: {desc}`（`agent/skill_utils.py:399`） |
| 描述上限 60 | `SKILL_PROMPT_DESC_LIMIT=60`，`desc[:57]+"..."`（`agent/skill_utils.py:741,753`） | ✅ 已抄（02c61d5）：`truncate_description`（`agent/skill_utils.py:360-369`，含省略号共 60） |
| 正文只经 `skill_view` | 模块 docstring 明说 progressive disclosure（`tools/skills_tool.py:1-6`） | ✅ 已抄（02c61d5）：`Skill` 无正文字段（`agent/skill_utils.py:90-101`）、正文由 `load_skill_body`（`:338-356`）按需读 |
| 索引放 volatile 首块（复用前缀） | `agent/system_prompt.py:705-708`（*the index leads the volatile band*） | ✅ 已抄（02c61d5）：`agent/system_prompt.py:303-306`（`_skill_index_parts` 排最前） |
| 不分页硬规矩 | `skills/AGENTS.md:57`（*No offset/limit pagination on skill-loading tools*） | ✅ 已对齐：`skill_view` schema 只有 `name`（`mini-hermes/tools/skills.py:64-83`），正文一次全文 |
| 磁盘快照 + 进程内 LRU | `agent/prompt_builder.py:1106-1125`（`.skills_prompt_snapshot.json` + `_SKILLS_PROMPT_CACHE`） | ⬜ 没做（量级）：每次**真读盘**（`agent/skill_utils.py:288-317`），无缓存；等真慢了再补 |
| `skills.auto_load` 钉进 stable 前缀 | `agent/system_prompt.py:316-320, 684-685` | ⏭ 不抄（无 auto_load 配置面） |

### 带上辅助脚本（Include Helper Scripts）

**① Hermes 怎么做的**：遇到 XML/JSON 解析或复杂逻辑，把辅助脚本放进技能的 `scripts/`——**不要指望 LLM 每次调用都现场手写解析器**。这不是提示，是作者规范里的硬性条目：`scripts/`、`references/`、`templates/` 是随包目录，辅助脚本按 **skill-relative 路径**引用，让非平凡逻辑从模型手里挪进文件。

**② 读 Hermes 的代码**：`skills/AGENTS.md:50-51`（作者规范第 6 条，逐字）：

```text
50: 6. **`scripts/`, `references/`, `templates/`.** Don't make the model inline-write parsers or
51:    non-trivial logic every call — ship a helper script and reference it by skill-relative path.
```

要害：规则一句话——*ship a helper script*——落脚点是把**非平凡逻辑固化进文件**，而不是每轮让模型重写一遍解析器。这与渐进披露同一套逻辑：目录里的重物**不进提示词**，只作"第二跳"入口（`references/`/`scripts/` 从不被当成独立技能扫描）。

**③ mini 的实现（现状）**：✅ **已抄（第二跳入口）**、⬜ 没做（随包脚本实体）。mini 的 `SUPPORT_DIRS = ("references", "templates", "assets", "scripts")`（`agent/skill_utils.py:81-86`）目前**只用于说明**——因为 mini 只扫一层，这个常量刻画的是"这些目录是第二跳、不是独立技能"这条设计；而 `skill_view` 的返回里带了**技能目录条目列表**（`mini-hermes/tools/skills.py:120-135`，上限 200 条），让 `references/`/`scripts/` 里的文件**可见**，模型由此自己决定要不要再要一份。至于"随包脚本实体"，mini 目前没有带 `scripts/` 的技能样例，故这一档只落在"入口在、内容空"。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 辅助脚本 | 随包 `scripts/`，正文按 skill-relative 路径引用（`skills/AGENTS.md:50-51`） | ⬜ 没做（量级）：`SUPPORT_DIRS` 常量在（`agent/skill_utils.py:81-86`），无随包脚本实体 |
| 第二跳入口可见性 | `linked_files` 列出 `references/`/`scripts/`（`tools/skills_tool.py:377` `_skill_linked_files`） | ✅ 已抄（02c61d5）：`skill_view` 返回目录条目列表（`mini-hermes/tools/skills.py:120-135`） |

### 以文档形式投递媒体（Deliver media as documents · `[[as_document]]`）

**① Hermes 怎么做的**：若技能产出高分辨率截图、图表，或任何"**有损预览压缩会造成伤害**"的图像，就在响应某处（通常是最后一行）发出**字面指令** `[[as_document]]`。网关会**剥掉**这条指令，并把该次响应里抽取到的每一个媒体路径**作为可下载的文件附件**投递，而不是内联的图片气泡。一句话：这条指令改变的是**媒体走文本气泡还是走文件附件**这条投递路径。

**② 读 Hermes 的代码**：`gateway/platforms/base.py:4084-4085`（媒体分流入口，逐字）：

```text
4084:        # Captured before extract_media strips it: images then go via send_document (no recompression).
4085:        force_document = "[[as_document]]" in response
```

要害：关键在注释那半句——`[[as_document]]` 必须在 `extract_media` **剥掉它之前**被捕获（`:4084`），于是这一轮的图片改走 `send_document`，**不再重压缩**。这正是文档"有损预览压缩会造成伤害"的代码落点：指令本身会被剥掉（不作为文本流出），留下的效果是投递方式从"内联缩略"换成"原文件附件"。

**③ mini 的实现（现状）**：⏭ **不抄——mini 网关没有媒体投递面**。第 7 / 12 章已确认 mini 的网关汇报面只有文本投递（`gateway/delivery.py::deliver`），没有 `[[as_document]]` / `[[audio_as_voice]]` 这类指令解析、也没有 `send_document` 这条附件路径。技能输出到这里仍只是普通文本。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| `[[as_document]]` | 网关剥离指令、媒体改走 `send_document`（`gateway/platforms/base.py:4084-4085`） | ⏭ 不抄（mini 网关无媒体投递面） |

#### 从 SKILL.md 引用随包脚本（Referencing bundled scripts from SKILL.md）

**① Hermes 怎么做的**：技能被加载时，激活消息把技能的**绝对目录**暴露成 `[Skill directory: /abs/path]`，并在 SKILL.md 正文的**任意位置**替换两个模板 token：`${HERMES_SKILL_DIR}` → 该技能目录的绝对路径；`${HERMES_SESSION_ID}` → 当前活跃会话 id（**没有会话时原样保留**）。于是 SKILL.md 可以让 agent 直接跑一个随包脚本（`node ${HERMES_SKILL_DIR}/scripts/analyse.js <input>`），agent 看到的就是替换后的可运行命令——**不必做路径运算、也不必多跑一趟 `skill_view`**。用 `config.yaml` 里的 `skills.template_vars: false` 可全局关掉替换。

**② 读 Hermes 的代码**：`agent/skill_preprocessing.py:13-15`（逐字注释 + 正则）：

```text
13: # ${HERMES_SKILL_DIR} / ${HERMES_SESSION_ID} tokens. Unresolvable ones (e.g. no
14: # session) are left as-is so the author can spot them.
15: _SKILL_TEMPLATE_RE = re.compile(r"\$\{(HERMES_SKILL_DIR|HERMES_SESSION_ID)\}")
```

要害：注释逐字说明"**解析不出来的 token 原样留着**"（`:13-14`）——"没有会话时原样保留"不是疏漏，而是**故意让作者自己看得见**那个没被替换的值；替换实现里同一句被写成 *tokens without a value stay in place*（`:35`）。开关的默认值是**开**：`cfg.get("template_vars", True)`（`:99`），关它要显式写 `skills.template_vars: false`。

**③ mini 的实现（现状）**：⏭ **不抄（有更窄的替代）**。mini 的 `skill_view` 直接在返回里给出 `path` 字段（`mini-hermes/tools/skills.py:117`，技能文件的绝对路径），模型据此也能拼出"跑随包脚本"的命令；但 mini **没有** `${HERMES_SKILL_DIR}` / `${HERMES_SESSION_ID}` 的模板替换面，激活消息也没有 `[Skill directory: …]` 这一块——这条更窄的替代覆盖了"拿到绝对路径"这个需求，没有覆盖 `HERMES_SESSION_ID`。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 模板 token 替换 | `${HERMES_SKILL_DIR}` / `${HERMES_SESSION_ID}`（`agent/skill_preprocessing.py:15`；开关默认开 `:99`） | ⏭ 不抄（`skill_view` 只回 `path`，无模板替换） |
| 激活消息 `[Skill directory: …]` | `agent/skill_commands.py:246-248` | ⏭ 不抄（无激活消息脚手架） |

#### 内联 shell 片段（Inline shell snippets (opt-in)）

**① Hermes 怎么做的**：技能还能在 SKILL.md 正文里嵌写作 `` !`cmd` `` 的内联 shell 片段。启用后，每个片段的 stdout 会在 agent 读到它**之前**被内联进消息，于是技能能注入动态上下文——例如 `Current date: !\`date -u +%Y-%m-%d\``、`Git branch: !\`git -C ${HERMES_SKILL_DIR} rev-parse --abbrev-ref HEAD\``。这**默认关闭**：SKILL.md 里任何片段都会**在主机上、不经审批地**运行，所以只对**你信任的技能来源**启用。片段以技能目录为工作目录运行，输出上限 4000 字符；失败（超时、非零退出）以一个简短的 `[inline-shell error: …]` 标记出现，而不会把整个技能弄坏。

**② 读 Hermes 的代码**：`agent/skill_preprocessing.py:98-102`（预处理入口的两个开关，逐字）：

```text
98:    cfg = skills_cfg if isinstance(skills_cfg, dict) else load_skills_config()
99:    if cfg.get("template_vars", True):
100:        content = substitute_template_vars(content, skill_dir, session_id)
101:    if cfg.get("inline_shell", False):
102:        content = expand_inline_shell(content, skill_dir, int(cfg.get("inline_shell_timeout", 10) or 10))
```

要害：两个开关的**默认值刻意不对称**——`template_vars` 默认 `True`（`:99`），`inline_shell` 默认 `False`（`:101`）。理由写在配置默认值的注释里：内联 shell 是"**技能作者的内容在主机上、不经审批地跑**"，只该对信任来源开（`hermes_cli/config_defaults.py:1367-1369`）。同一处也钉了另外两个常量：输出上限 `_INLINE_SHELL_MAX_OUTPUT = 4000`（`:19`）与每片段超时默认 10 秒（`hermes_cli/config_defaults.py:1370`）。

**③ mini 的实现（现状）**：⏭ **不抄——mini 没有"技能正文在主机执行"这个面**。mini 的技能正文只在 `skill_view` 调用时**作为字符串**返回给模型，没有任何地方会把 SKILL.md 的内容拿去 `subprocess` 执行；本机命令执行的唯一入口是显式工具 `terminal`（且走审批）。所以"内联 shell 默认关、只对信任来源开"这条安全协商，在 mini 里连协商的对象都不存在。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 内联 shell | opt-in（默认关 `agent/skill_preprocessing.py:101`；`hermes_cli/config_defaults.py:1369`），4000 上限、`[inline-shell error: …]` | ⏭ 不抄（mini 无"技能正文在主机执行"面；`terminal` 是显式审批工具） |

### 跑一遍试试（Test It）

**① Hermes 怎么做的**：文档给的验收动作是"跑一遍"——`hermes chat --toolsets skills -q "Use the X skill to do Y"`，看 agent 是否正确遵循了指令。比手动跑更硬的是作者规范：**每个技能都要配一个测试文件**，要求只用 stdlib + pytest + `unittest.mock`、**不联网**。

**② 读 Hermes 的代码**：`skills/AGENTS.md:52-53`（作者规范第 7 条，逐字）：

```text
52: 7. **Tests at `tests/skills/test_<skill>_skill.py`**, stdlib + pytest + `unittest.mock` only, no
53:    live network. Run `scripts/run_tests.sh tests/skills/test_<skill>_skill.py -q`.
```

要害：真实侧的"跑一遍"不止于手动 CLI 调用，还有一条**硬性测试规范**——每技能配 `tests/skills/test_<skill>_skill.py`，只用 stdlib + pytest，**不联网**。这条把"跑一遍"从一次性人工动作升级成**可重复的契约**，也正是 mini 这一章照做的体例。

**③ mini 的实现（现状）**：✅ **真的做了（02c61d5）**，两件真跑：

- `tests/test_skills.py`（691 行 / **7 组 / 103 条全过**）：stdlib、**不联网**、`MINI_HOME` 与**内置根**（patch `bundled_skills_dir`）都在 import 前/夹具里指到临时目录（否则测试会读用户真实的技能库、以及仓里随程序发布的内置技能）。组 1 钉索引字节稳定、组 2 钉"正文只经 `skill_view` 出现"、组 5 钉 `skill_view` 安全边界（`..` / 绝对路径 / 符号链接目录 / 库外文件全部拒读）。
- `demos/ch13_real_run.py`（**17/17**）：把**真提示词**建出来再在里面搜正文独有句，是"演示证据"而非"调函数看返回值"。
- 缺的 CLI 面：mini **没有** `hermes chat --toolsets skills` 这个 `--toolsets` 参数（只有 `--list-toolsets` 列表与 REPL 里的 `/tools`，`mini-hermes/hermes_cli/main.py:73-77, 125-128`）——所以 mini 的"跑一遍"落在上面的脚本与测试上，而不是一个 `--toolsets` 旗标。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 硬性测试规范 | 每技能配 `tests/skills/test_<skill>_skill.py`，stdlib + pytest，不联网（`skills/AGENTS.md:52-53`） | ✅ 已抄（02c61d5）：`tests/test_skills.py`（stdlib、不联网、6 组 / 72 条） |
| 手动验收 CLI | `hermes chat --toolsets skills -q "…"` | ⬜ 没做（无 `--toolsets` 参数）：替代物 `demos/ch13_real_run.py`（17/17）+ `--list-toolsets`（`hermes_cli/main.py:74`） |

---

## §六 技能该住在哪里？（Where Should the Skill Live?）

**① Hermes 怎么做的**：技能该住哪，文档给的是**按"普遍有用程度"三分**——随包内置（`skills/`，对大多数用户普遍有用：文档处理、网页研究、常见开发工作流、系统管理）· 官方可选（`optional-skills/`，官方有用但并非人人需要，例如付费服务集成或重依赖，经 `hermes skills browse` 标为 "official"、以内置信任安装）· 技能中心（Skills Hub，专用 / 社区贡献 / 小众，上传注册表后经 `hermes skills install` 分享）。

但"住在哪"在**实现**上还有一层更硬的含义：它是**发现优先级 + 信任决定**。发现分两级"表面"（`skills/` 默认加载、`optional-skills/` 需激活）与四层目录（本地 `~/.hermes/skills/` → `skills.create_dir` → `skills.external_dirs` → 项目内目录）；全局优先级排序是 `project → local → external`，**首见胜出**。其中最要紧的一条：**项目内技能默认不加载**——它们是"prompt-injection 向量"，只有在 root 被列进 `skills.trusted_project_dirs` 时才生效，且此时会覆盖同名的 profile/内置技能。

**② 读 Hermes 的代码**：`agent/skill_utils.py:409-413`（项目技能的信任门，逐字注释）：

```text
409: # Project-local skills (<root>/.hermes/skills, <root>/.agents/skills; root = nearest
410: # .git ancestor) are a prompt-injection vector if auto-sourced from any clone, so
411: # they load only when the root is in ``skills.trusted_project_dirs``; then they
412: # override same-named profile/bundled skills. cwd + trust list are session-fixed
413: # so the skills index stays byte-stable.
```

要害："住在哪"不只是"有用没用"，还是一个**信任决定**——从一个 clone 自动加载的项目技能是注入向量，所以默认**不加载**，只有 root 进了 `skills.trusted_project_dirs` 才生效（`:410-411`）；而且 `cwd + trust list` 是**会话固定**的，这样技能索引才能字节稳定（`:412-413`）。**发现范围的定义同时是攻击面的定义**——这是本节最该记住的一句。

**③ mini 的实现（现状）**：✅ **已抄**（`02c61d5` 的"与 profile 的关系" + `4b0c96a` 的**第二个发现根**：内置 `<repo>/mini-hermes/skills/` 与用户根共存、用户优先）；⏭ **不抄**信任门（判据见下一条：mini 没有从任意项目自动发现技能的路径）。

- **每个 profile 一套技能**：`mini-hermes/hermes_cli/profiles.py:43` 把 `skills` 加进 `PROFILE_DIRS = ["logs", "hooks", "skills"]`——新建 profile 时就建出该目录（注释逐字：*第 13 章才补进来：在此之前技能库没有消费者，所以刻意不建*）。路径来源是 `mini-hermes/agent/skill_utils.py:121-130` 的 `skills_dir()` = `get_mini_home() / "skills"`。因为**一个 profile = 一个 home**，所以"技能属于谁"这件事**一行都不用另写**——`get_mini_home()` 自动隔离，天然不串味。
- **只扫一层**：mini 只扫 `skills/<name>/SKILL.md`（`mini-hermes/agent/skill_utils.py:29-31` 的 docstring 逐字：*只扫一层（真实 Hermes 会递归走分类目录，如 `skills/github/xxx`）*）。★ **两个根都如此** —— 第 13b 加的是"去哪儿找"的第二个根，不是 `<category>/` 那层深度，"多根 ≠ 多层"。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 两级目录面 | `skills/` 默认加载 + `optional-skills/` 需 `hermes skills install`（`skills/AGENTS.md:6-15`） | ⬜ 没做（量级）：`optional-skills/` 那种**激活策略**面没有消费者；★ 但**来源**面的第二级已补（`4b0c96a`）：内置根 + 用户根共存、用户优先（`mini-hermes/agent/skill_utils.py:172` 的 `skill_roots()`） |
| 发现优先级 | four layers + `project → local → external`，首见胜出（`agent/skill_utils.py:398-406, 802-810`） | ✅ 已抄（单层）：`iter_skill_dirs` 排序后扫（`agent/skill_utils.py:133-152`）；同名"首见胜出"（`:296-297`） |
| 项目内技能的信任门 | 默认不加载，须入 `skills.trusted_project_dirs`（`agent/skill_utils.py:409-413`） | ⏭ 不抄（无 `trusted_project_dirs` 配置面） |
| 与 profile 的关系 | 每 profile 一套（`_PROFILE_DIRS` 含 `skills`，`hermes_cli/profiles.py:32`） | ✅ 已抄（02c61d5）：`PROFILE_DIRS` 含 `skills`（`hermes_cli/profiles.py:43`），路径走 `get_mini_home()/skills`（`agent/skill_utils.py:121-130`） |
| `optional-skills/` 的信任 | `official` provenance → `builtin` 信任（`tools/skills_guard.py:780-788`；`skills/AGENTS.md:8-12`） | ⏭ 不抄（无 hub / 可选技能面） |

## §七 蓝图：同时也是自动化的技能

**① Hermes 怎么做的**：蓝图（blueprint）是这几节里最像"新机制"的一节——文档为它给了一个 YAML 块、一个安装动作、一条 `/suggestions` 接受流程。但源码第一句就把这个幻觉打掉了：*A blueprint is NOT a new object type*。蓝图**就是**一个普通 `SKILL.md`，只不过 frontmatter 里多了一个 `metadata.hermes.blueprint` 块。

判断规则是**结构式**的，不是名字式或目录式的：`metadata.hermes.blueprint` 是 mapping、且有非空的 `schedule`，这个技能**就是**蓝图；块存在但结构不合法 → 抛 `BlueprintError`（让打错的字段浮出来，而不是静默变成一个"什么也不做"的技能）。除 `schedule` 外全是可选项：`deliver`（默认 `origin`）、`prompt`、`no_agent`、`model`、`provider`、`enabled_toolsets`。

**为什么不做成新对象类型**，才是这一节真正要讲的设计决策。做成新类型，意味着要把它需要依附的每一样东西**再造一遍**：新的存储格式、新的发现路径、新的安装事务、新的安全扫描、新的信任分级、新的分发通道、新的索引与缓存。而技能管线里这些东西**已经都有**，且刚刚在本章前几节被逐一讲清。把蓝图做成"声明了某个块的普通技能"，它就能**原封不动**流过整条管线——搜索、检视、安装、安全扫描、来源追溯、tap、集中式索引、`hermes skills publish`，"没有新东西要学"（文档原话）。这不是省事，是**少一条并行路径**：本项目已反复踩过"同一事实写两处 → 漂移"（工具分发、结果回填、状态重建）。用一个 frontmatter 块当载体，那些能力是**免费继承**的，不存在第二条要保持同步的代码路径。

于是 `tools/blueprints.py` 自己的职责被压到极小：它**只做翻译**——把那个块翻成 cron 的 `create_job()` 参数，以及反向把 job 翻回一个可发布的 `SKILL.md`（`export_blueprint`，会重新写出 `metadata.hermes.blueprint` 并打上 `tags: [blueprint, automation]`）。安装一个蓝图时，它被登记成一条**建议**，从不自动排程——`register_blueprint_suggestion` 的 docstring 逐字是 *never auto-scheduled; the user accepts or dismisses it*。翻译**只有一处**（`blueprint_to_job_spec`），`create_blueprint_job` 与建议路径共用它——防的正是两条路各写一份译法然后漂移。

还有一层是"给人看的门面"：`cron/blueprint_catalog.py` 把一个自动化定义成一份 slot schema，dashboard 表单、`/blueprint` 斜杠命令、agent 种子提示、文档深链**共用同一个定义**渲染。它的 docstring 点明了为什么这么切：*Users never type raw cron: schedule_template fixes the recurrence; only human parts (time, weekdays) are slots.* —— 用户永远不手写 cron 表达式，可变的部分只剩"几点、周几"这类人类参数。

**② 读 Hermes 的代码**：`tools/blueprints.py:1-7`（模块 docstring，逐字——"不是新对象类型"这句就是全节的卖点）：

```
"""Blueprints: shareable plain-language automations layered on skills + cron.

A blueprint is NOT a new object type: it is an ordinary skill whose frontmatter declares
``metadata.hermes.blueprint`` (``schedule`` required; optional ``deliver`` [default "origin"],
``prompt``, ``no_agent``, ``model``, ``provider``, ``enabled_toolsets``), so it rides the whole
skills-hub pipeline for free. This module only bridges that block to cron ``create_job()``,
plus the inverse (``export_blueprint``) back to a SKILL.md.
"""
```

**③ mini 的实现（现状）**：⏭ **不抄——mini 没有落地面**。理由不是"难"，是**没有可翻译的对象**：蓝图对外是一个声明，对内是一个 cron job，而 mini 没有 cron——没有 job 引擎、没有调度器、没有后台产出的消费者。第 12 章刚确认过这个事实，`gateway/run.py` 的维护 tick 表也把它写死了：对应「cron 滴答」那一行是「❌ 没有 scheduler」。一份"把声明翻成 job"的翻译层，在 mini 里没有被翻译的东西。

顺带记一句：mini **显式知道**自己没有 cron，不是遗漏。`agent/system_prompt.py` 讲"跳过 cwd 指令但保留人格"那个开关时，明说那个场景（cron 任务）*代码里没露出来，是因为 mini 现在还没有 cron / 子 agent*。所以这一节对 mini 的价值是**一条被记下的判据**：等哪天真长出后台排程，正确的抄法是加**翻译层**（块 ↔ job），而**不是**加一种新对象类型。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 蓝图的对象模型 | 普通 `SKILL.md` + `metadata.hermes.blueprint` 块，**无新类型**（`tools/blueprints.py:1-7`） | ⏭ 不抄（无 cron / job 引擎可翻译） |
| 块 → job 的翻译 | `blueprint_to_job_spec`（`tools/blueprints.py:118-128`），全章唯一翻译点，两条路径共用防漂移 | ⏭ 不抄 |
| 安装即登记建议 | `register_blueprint_suggestion`（`tools/blueprints.py:140-161`），**永不自动排程** | ⏭ 不抄（无建议面，见 §八） |
| 人类友好的 slot 目录 | `cron/blueprint_catalog.py:1-6`，用户永不手写 cron | ⏭ 不抄 |

---

## §八 建议的定时任务

**① Hermes 怎么做的**：Hermes 有一个统一的"提议面"——`/suggestions`。它要解决的问题是：自动化常常**在用户还没想到时就先被工具看见了**（你接上 Gmail，显然该有个每日邮件摘要；你反复问同一件事，显然该排个程），但"显然"不能自动变成"已排程"。于是所有来源的提议都流经**同一个面**，由用户一点即受。

四条来源是一张**白名单枚举**：`catalog`（策展的起步自动化）、`blueprint`（装了带 `blueprint:` 块的技能）、`usage`（后台自改进评审）、`integration`（接上了某个账号）。写错 source 直接 `ValueError`——枚举是硬边界，不是建议。

这个面有两条核心不变式。其一，**consent-first**：建议**永不**自动创建任务，接受永远显式（docstring 逐字 *nothing auto-creates (consent-first)*）；接受时调用的是 `cronjob` 工具所用的**同一个** `cron.jobs.create_job`——没有第二套任务引擎。这与 §七"不做新对象类型"是同一条纪律：能复用的引擎绝不新起一个。其二，**驳回即闩锁**：一条建议被驳回后按 `dedup_key` 记下，**永不重提**；待办列表封顶 `MAX_PENDING = 5`，注释写明理由是 *so the list never becomes a nag wall*（免得它变成一堵唠叨墙）——满了之后新建议被**静默丢弃**，而不是排队等着烦你。存储镜像 `cron/jobs.py`（原子替换、0600），落在 `~/.hermes/cron/suggestions.json`。

文档还给了一个"模式"级的例子：`catalog` 里的**重要邮件监控** = 轮询 → 分类 → 上浮。它用一个便宜的分类器模型（`config.yaml` 里的 `auxiliary.monitor`）给收件箱条目打分，只投递超过紧急阈值的那些，其余保持沉默。这说明建议承载的不只是"排个定时任务"，而是可以在 job 里嵌一条"先打分、再决定要不要吵你"的管线。

**② 读 Hermes 的代码**：`cron/suggestions.py:1-9`（模块 docstring，逐字——四来源、consent-first、无第二套引擎三件事都在这里）：

```
"""Suggested cron jobs — proposed automations the user accepts with one tap.

A suggestion is a ready-to-run cron job spec the user accepts (creates the real job) or dismisses
(latched by ``dedup_key`` so it is never re-offered). Every proposal flows through here regardless
of source: ``catalog`` (curated starters), ``blueprint`` (skill ``blueprint:`` blocks, see
``tools/blueprints.py``), ``usage`` (self-improvement review), ``integration`` (connected account).
Accepting calls ``cron.jobs.create_job`` with the stored ``job_spec`` — no second job engine;
nothing auto-creates (consent-first). Storage mirrors ``cron/jobs.py`` (atomic replace, 0600).
"""
```

**③ mini 的实现（现状）**：⏭ **不抄——mini 没有 cron**。"建议"本质上是一个**动作面**：它必须能把一条"准备好了的 job spec"变成一个**真的在跑的任务**。mini 没有这个终点——没有 cron、没有 job 引擎、没有调度器（`gateway/run.py` 的维护 tick 表里「cron 滴答」那行是「❌ 没有 scheduler」）。没有"接受"这个动作可以落的地方，提议面就没有存在意义。

不过这一节有一个**形状**值得记下，因为它与 mini 的既有哲学同源：`VALID_SOURCES` 是一个**白名单 frozenset**（`cron/suggestions.py:39`），写错的值不是"被忽略"而是直接报错；`MAX_PENDING` 封顶则是对"别烦用户"这条约束的**硬编码**。「把边界写成代码、而不是靠约定」在 mini 里有同源的例子——`toolsets.py` 那句"按名字列清单会过期、按 toolset 才自动跟上"，以及 `skill_view` 的可见性与提示词索引**同开同关**（`check_skills_available` 调 `skill_utils.has_skills()`，两处问同一个函数）。所以这一节对 mini 是**反面参照**：等真长出后台排程时，"提议 ≠ 排程"这条 consent-first 是必须原样搬过来的第一条规矩。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 统一的 `/suggestions` 面 | 四来源（catalog / blueprint / usage / integration）共用一个面 | ⏭ 不抄（无 cron / job 引擎） |
| consent-first | 建议永不自动创建；接受显式调 `cron.jobs.create_job`，无第二套引擎 | ⏭ 不抄（没有"接受"这个动作的落点） |
| 驳回闩锁 + 封顶 | `dedup_key` 闩锁永不重提；`MAX_PENDING = 5` 防唠叨墙（`cron/suggestions.py:36-39`） | ⏭ 不抄 |
| 白名单来源枚举 | `VALID_SOURCES` frozenset（`:39`），写错即报错 | 已对齐（同源纪律：`toolsets.py` 白名单 / `skill_view` 与索引同开同关），但无建议面 |

---

## §九 发布技能

**① Hermes 怎么做的**：发布在 Hermes 里**不是一套新协议**——它就是"把一个本地技能目录送到某个 registry"，而 registry 的形态只有两种：内置的技能中心（hub），或一个被登记为 tap 的自定义仓库。两者都只是**分发通道**的选择，技能本身仍是那个 `SKILL.md`。发布**不**改变技能的对象模型（与 §七同一条纪律），它只改变"这个文件从哪来、装的人凭什么信它"。

### 发布到技能中心（To the Skills Hub）

`hermes skills publish skills/my-skill --to github --repo owner/repo`。CLI 面上 `--to` 只有两个取值：`github`（默认）与 `clawhub`；`--repo` 是目标 GitHub 仓库。实现里发布是一条**带前置门的流水线**：先要求 `SKILL.md` 有 `description`（没有就不发），然后**发布前先扫**（`scan_skill(path, source="self")`），`dangerous` 裁决当场拒发。这一条把"发布"和"安装"接上了同一套扫描器（见 §十）：你装进来的东西被扫，你发出去的东西也被扫，用的是同一个 `scan_skill`。`--to github` 走 fork + branch + PR（`_github_publish`），并要求 GitHub 认证（`GITHUB_TOKEN` 或 `gh auth login`）。`--to clawhub` 目前**未实现**：程序会明确打印 *ClawHub publishing is not yet supported*，只给一个手动提交地址 `https://clawhub.ai/submit`。

### 发布到自定义仓库（To a Custom Repository）

把自己的仓库加成某个 tap：`hermes skills tap add owner/repo`。tap 由一个 `TapsManager` 管（add / list / remove），它把"一个 GitHub repo"登记成**技能源**；之后用户就能从你的仓库里搜索并安装。

这条"来源"通道并不只有 tap 一种。文档把 Hermes 现在能消费的外部第三方技能归纳为**三种发现模型**（原话）：直接 GitHub 标识（如 `openai/skills/k8s`）、`skills.sh` 标识（如 `skills-sh/vercel-labs/json-render/json-render-react`）、以及从 `/.well-known/skills/index.json` 提供的 well-known 端点。前两者各有专门适配器，第三种对应 `tools/skills_hub_sources.py` 里的 `WellKnownSkillSource`——`BASE_PATH = "/.well-known/skills"`，抓到的技能带 `source="well-known"`、`trust_level="community"`。换句话说：**发布侧的通道越多，信任分级就越不能省**——每一条新通道都得先回答"这东西该算哪一档信任"（四档矩阵见 §十）。

**② 读 Hermes 的代码**：`hermes_cli/subcommands/skills.py:148-153`（`publish` 子命令的参数面，逐字——"发布到哪"这件事在这里就被枚举死了）：

```
    skills_publish = skills_subparsers.add_parser("publish", help="Publish a skill to a registry")
    skills_publish.add_argument("skill_path", help="Path to skill directory")
    skills_publish.add_argument(
        "--to", default="github", choices=["github", "clawhub"], help="Target registry")
    skills_publish.add_argument(
        "--repo", default="", help="Target GitHub repo (e.g. openai/skills)")
```

**③ mini 的实现（现状）**：mini 没有技能发布、下载和安装事务。当前发现有两个来源根：随程序发布的内置根，以及每个 profile 的用户根；用户同名技能优先。把文件放入用户根只完成本机发现，不等于发布给他人。已有模型 HTTP 与网页搜索不能替代技能分发流程。

普通发现仍遵循渐进披露：索引列名字与描述，正文由 `skill_view` 按需读取。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 发布到技能中心 | `hermes skills publish --to github` 走 fork+PR；**发布前先扫**、`dangerous` 拒发；`clawhub` 未实现（`hermes_cli/subcommands/skills.py:148-153`、`hermes_cli/skills_hub.py:1143-1188`） | ⏭ 不抄（无网络客户端封装与安装事务） |
| 发布到自定义仓库 / tap | `hermes skills tap add owner/repo`（`TapsManager`，`hermes_cli/skills_hub.py:1108-1135`） | ⏭ 不抄 |
| 三种发现模型 | 直接 GitHub 标识 / `skills.sh` / well-known 端点（`tools/skills_hub_sources.py:21-25`） | ⏭ 不抄（无远程来源） |
| 技能入库（**更窄的替代**） | 下载 → 解包 → 扫 → 信任判决 → 原子落盘，走整条 hub 管线 | 已对齐的终点：手写 `<MINI_HOME>/skills/<name>/SKILL.md`，**发现 = 扫目录**（`agent/skill_utils.discover_skills()`，`02c61d5`） |

## §十 安全扫描（Security Scanning）

**① Hermes 怎么做的**：技能是**可执行的说明书**——模型会照着它去跑 shell、读文件、调工具。只要技能有一个来源是"陌生人"，这件事就不再是"文档质量"问题，而是**供应链**问题。所以 Hermes 对技能的处置分成两层：**扫内容**（`skills_guard` 的正则静态扫描）与**定信任**（按来源给一个信任级别，再拿级别去查放行策略）。这两层是分开的，且**信任不是"扫完再打分"，是"从来源一次决定"**——`_resolve_trust_level`（`tools/skills_guard.py:780-788`）的 docstring 逐字写的是 *"Source id -> trust level."*，它吃的是 `source` 字符串（`"official"`、`"agent-created"`、`github` 标识…），输出一个信任级别。同一份技能内容，从 `openai/skills` 装进来与从某个陌生仓库装进来，扫出来的发现（`Finding` 列表）一模一样，但**判决不同**——差别全在信任档上。

判决链有两步。**第一步把发现压成裁决**：`_determine_verdict`（`:791-794`）逐字 `return "dangerous" if "critical" in sev else "caution" if "high" in sev else "safe"`——`critical` → `dangerous`、`high` → `caution`、**只有 `medium`/`low` 时算 `safe`**（docstring 逐字 *"medium/low alone are informational (safe)"*）。**第二步拿（信任级别, 裁决）去查策略矩阵**，也就是下面 ② 引的那张 `INSTALL_POLICY`。四档信任里有一档要单独说：`builtin` 的安装策略是始终允许；扫描入口仍可能被安装或审计路径调用——模块 docstring（`:4`）逐字 *"builtin (never scanned)"*，矩阵里对应的三个格子全是 `"allow"`。随包来源 `official` 被判为 `builtin`；不要把模块注释的 never scanned 当作所有调用路径都会跳过扫描的保证。

`--force` 是本节最容易读漏边界的地方。它不是"万能覆盖"：`should_allow_install`（`:660-673`）先算一个 `hard_block`（`:665` 逐字 `hard_block = result.verdict == "dangerous" and result.trust_level in ("community", "trusted")`），再逐级判——`:668` 是 `if force and not hard_block:`，也就是说**`--force` 能覆盖除"`community`/`trusted` 上的 `dangerous`"以外的每一个 block**；docstring（`:661-662`）把这条写死了：*"*force* overrides every block except a dangerous verdict on community/trusted sources."*。被硬挡住时返回的理由（`:673`）逐字是 `"--force does not override a dangerous verdict."`——用户看到的**不是**"Use --force to override"（那是可被覆盖时的文案）。docstring 前半句还点明了查表口径，**默认兜底落在查表那一行**（`:663` 逐字 `decision = INSTALL_POLICY.get(result.trust_level, INSTALL_POLICY["community"])[VERDICT_INDEX.get(result.verdict, 2)]`）：`trust_level` 查不到时按 `community` 处理——**未知来源不会意外获得宽松待遇**。

第三档 `agent-created` 的判决值不是 allow/block，而是 **`"ask"`**（`:32`）：docstring（`:662`）逐字 *"``allowed`` is None when policy says \"ask\"."*——回给调用方的是一个 `None`，含义是**把这条发现当"报错"交还给 agent，让它删掉惹事的内容再重试**（`:31` 注释逐字 *"\"ask\" = error to the agent (retry without the flagged content)"*）。而且这一档**默认不开**：它是 opt-in，只在 `skills.guard_agent_created` 为真时才生效，配置默认值逐字是 `"guard_agent_created": False,`（`hermes_cli/config_defaults.py:1374`）。一个 Agent 自己 `skill_manage(create)` 写出来的技能，在默认配置下**不会被拦**——这条不写清楚，读者会以为"agent 自建的技能一定过安检"。

**扫描时机不是一处，是五处**（每一处语义不同，这正是本节的信息量所在）：① hub 安装——先 `quarantine_bundle` 到隔离区再扫（`hermes_cli/skills_hub.py:628-641` `_scan_quarantined`），放行闸口在同一条路径上（`:721-723` 逐字 `allowed, _reason = should_allow_install(result, force=force)`，`--force` 就是从这里进来的）；② 已装技能复查——`hermes skills audit`（`:905`，扫在 `:927`）；③ 发布前——`do_publish` 先扫，`dangerous` 直接拒发（`:1164-1168`）；④ agent 自建技能写后扫（`tools/skill_manager_tool.py:41`/`:50-72`，**opt-in 默认关**）；⑤ **项目技能的每一次读取**——这是最容易漏的一处，也是全章最漂亮的一条推理：`hermes skills trust` 是一个**仓库级、做一次**的决定，但仓库里的技能内容**会在每次 `git pull` 时从它脚下改变**，于是"已经信任过的仓库"里可以被注入一个恶意技能而**任何地方都不会再扫**（`agent/skill_utils.py:520-523` 的注释把这条的编号都写了：`#48974`）。对策是**每个项目 `SKILL.md` 的父目录都过一次 hub 同款扫描器**（内容 hash 缓存，`:523-527`），`dangerous` 直接隔离出索引/`skills_list`/`skill_view`/斜杠命令，`caution` 照常加载（与 hub 行为对齐，`:526-527`），而且**扫描器崩了也隔离（fail-closed）**（`:533-557` `is_quarantined_project_skill`）。五处之外还有一处**收口**：项目技能的迭代只有**一个**闸口 `iter_project_skill_files`（`:559-563`），隔离决定不许各消费面自行再判一遍。

扫描器本身的形状也值得一句。它是**正则静态扫描 + 结构检查**两层：正则表 `THREAT_PATTERNS`（`:114-390`，本轮实测 **131** 条），结构检查在 `_check_structure`（`:690-736`：超量文件、超大、二进制、可疑可执行位、逃逸 symlink），单文件扫描再叠一层**不可见 Unicode 隐写检测**（`:577-582`，命中判 `injection` / `high`）。`Finding` 的 `category` 字段（`:42`）注释只给了五个值加省略号，**实测真正的类别共 13 类**（见附录 C ①）。扫描结果按内容 hash 缓存，带版本号 `SCANNER_VERSION = "skills-guard-v5"`（`:21`），所以规则一改缓存自动失效。

**② 读 Hermes 的代码**：`tools/skills_guard.py:26-33`（`INSTALL_POLICY`，逐字）：

```text
26: INSTALL_POLICY = {
27:     #                  safe      caution    dangerous
28:     "builtin":       ("allow",  "allow",   "allow"),
29:     "trusted":       ("allow",  "allow",   "block"),
30:     "community":     ("allow",  "block",   "block"),
31:     # "ask" = error to the agent (retry without the flagged content); only when skills.guard_agent_created is on.
32:     "agent-created": ("allow",  "allow",   "ask"),
33: }
```

要害：**这张表就是"信任 × 裁决 → 动作"的全部权威**，四行三列十二个格子，列从左到右是裁决变坏，行区分来源信任（`agent-created` 另有重试策略）。三处细节只能在源码里读到、文档读不到：其一，**`trusted` 那一行并非全 allow**——`dangerous` 是 `"block"`，与 `community` 的 `dangerous` 一样被拦（`:29` 的第三个格子），这正是附录 C ② 要纠正的那半句；其二，**`agent-created` 的 `dangerous` 既不是 allow 也不是 block，是 `"ask"`**，第三种判决值，靠 `:31` 的注释定义其语义（不是拒绝，是"把发现当错误退给 agent 重试"）；其三，**`builtin` 的三个格子全是 `"allow"`，表示策略始终允许**（模块 docstring `:4` 逐字 *"builtin (never scanned)"*）——把这张表读成"所有技能都扫、只是放行尺度不同"就错了。表格右对齐的 `safe / caution / dangerous` 三列与 `VERDICT_INDEX = {"safe": 0, "caution": 1, "dangerous": 2}`（`:35`）是一一对应的，`:663` 的查表就是靠这个下标。

**③ mini 的实现（现状）**：mini 未实现技能内容扫描与按来源分级。内置根随程序发布，用户根由使用者管理，没有任意项目自动加载和 hub 安装流程；这是当前省略这些机制的范围依据，并不证明本地文件必然可信。

`agent/skill_utils.py` 与 `tools/skills.py` 只负责发现和读取，不能由此断言整个程序没有技能写入路径：通用文件工具和终端也能写文件，用户也可能复制第三方技能。读取技能不等于认可其所有指令，路径校验也不能检测正文里的恶意指令。

目前已做的是文件访问边界：名字只做相等比较，路径来自扫描结果；非法名字、符号链接和库外读取由发现与加载校验拒绝。内容信任与路径安全是两种不同的保障，不能互相替代。

**④ 与真实版的差别**：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 四档信任 × 三判决矩阵 | `INSTALL_POLICY`：`builtin/trusted/community/agent-created` × `safe/caution/dangerous`（`tools/skills_guard.py:26-33`） | ⏭ 不抄（内置根与用户根共存，未按内容来源作信任分级） |
| 来源 → 信任的解析 | `_resolve_trust_level`（`:780-788`）：`official`→`builtin`、`agent-created` 单列、其余看 `TRUSTED_REPOS` | ⏭ 不抄（未记录用户技能的分发来源） |
| 裁决映射 | `_determine_verdict`：`critical`→`dangerous`、`high`→`caution`、`medium/low` 单独出现→`safe`（`:791-794`） | ⏭ 不抄（无 `Findings`） |
| 正则扫描器 + 结构检查 | 131 条正则（`:114-391`）+ 结构检查（`:690-736`）+ 隐写检测（`:577-582`）；`Finding.category` 实测 **13 类** | ⏭ 不抄（尚未实现内容信任检查） |
| `--force` 与硬边界 | 覆盖除 `community`/`trusted` 的 `dangerous` 外所有 block（`:665`/`:668`/`:673`） | ⏭ 不抄（无 block 可覆盖） |
| 扫描时机（五处 + 收口） | 安装（`hermes_cli/skills_hub.py:628-641`/`:721-723`）· 审计（`:905`/`:927`）· 发布前（`:1164-1168`）· agent 自建 opt-in 写后（`tools/skill_manager_tool.py:41`/`:50-72`）· 项目技能每次读（`agent/skill_utils.py:519-557`） | ⏭ 不抄（五个时机的消费者全都不存在） |
| 写入面（"恶意的入口"） | `skill_manage` 可写技能；故须在写后扫（opt-in） | ❌ **没有**：`agent/skill_utils.py` / `tools/skills.py` grep 无任何写入 API，这两个技能模块只读；通用工具仍可写文件 |
| 「名字当路径」的安全边界 | `skill_view` 的路径解析 + 名字校验 | ✅ **已抄**：`is_valid_skill_name`（`agent/skill_utils.py:225-230`）+ 路径只来自扫描结果 + 符号链接发现阶段即拒（`tests/test_skills.py` 组 5 钉住） |
| 不可见 Unicode / 结构检查 | `scan_file`（`:554-582`）与 `_check_structure`（`:690-736`） | ⏭ 不抄（同上：尚未实现内容信任检查） |

---

## 附录 A · 引用汇编（正文没引、值得留痕的）

> 本附录把 §十 的「② 读 Hermes 的代码」单块塞不下的 `文件:行号` 集中归档，**按文件分组**（正文 ② 已引的 `INSTALL_POLICY` `:26-33` 不在此重复）。每条给：`文件:行号` + 该行/该段首 20 字 + 一句它证明什么。
>
> - **真实侧**锚 `f5d1926110`：`git -C ~/.hermes/hermes-agent show f5d1926110:<path>`
> - **mini 侧**行号按对应章节快照读取；历史实现落在 `02c61d5`：`sed -n 'Np' mini-hermes/<path>`
> - 行号应按对应提交的引文定位，不能把历史行号直接用于当前工作树；括号里的说明是「它在说什么」，不当逐字引文用。

### 真实侧（锚 `f5d1926110`）

**`tools/skills_guard.py`（811 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:1-7` | `"""Skills Guard — regex static scan of externally-sourced skills plus a trust-aware install policy.` | 扫描器自述：① 前提是 **externally-sourced**；② `builtin` **never scanned**；③ 管线是 `scan_skill` → `should_allow_install` → `format_scan_report`；④ 末尾自曝一条 **Known gap**（语言写 API 指向 agent 配置文件时只出低危 `*_ref` 发现） |
| `:23-24` | `# NVIDIA-verified skills each ship a signed skill.oms.sig ...` | 信任清单**四个**仓库（含 `NVIDIA/skills`，文档只列三个，见附录 C 补 ②） |
| `:38-46` | `@dataclass` / `class Finding:` | `Finding` 字段块：`severity`（`:41`）+ `category`（`:42`，注释只列五值加省略号）+ `file`/`line`/`match`/`description` |
| `:114` | `THREAT_PATTERNS = [` | 正则模式表起点（`:114-390`，本轮实测 **131** 条） |
| `:392` | `_COMPILED_THREAT_PATTERNS = [(re.compile(pattern, re.IGNORECASE), *rest) for pattern, *rest in THREAT_PATTERNS]` | 编译后的模式表——扫描循环遍历它（`:568`） |
| `:554-582` | `def scan_file(file_path: Path, rel_path: str = "") -> List[Finding]:` | 单文件扫描：docstring 行排除（`_compute_docstring_lines`）+ md 链接遮蔽（`_mask_prose_link_destinations`）+ 逐行正则 + `invisible_unicode` 隐写检测（`:580`） |
| `:586-602` | `def scan_skill(` | 扫描入口：结构检查 + 逐文件扫描 + 汇总 |
| `:629-650` | `def scan_skill_cached(` | 内容 hash + `SCANNER_VERSION` 缓存（`:644` 反序列化旧发现） |
| `:690-736` | 结构检查主体 | `structural` 五条（`oversized_file`/`binary_file`/`unexpected_executable`/`too_many_files`/`oversized_skill`）+ symlink 逃逸（判 `traversal`） |
| `:780-788` | `def _resolve_trust_level(source: str) -> str:` | source → trust：`agent-created`（`:784`）· `official`→`builtin`（`:786-787`）· 其余命中 `TRUSTED_REPOS` 则 `trusted`、否则 `community`（`:788`） |
| `:791-794` | `def _determine_verdict(findings: List[Finding]) -> str:` | 裁决映射：`critical`→`dangerous`、`high`→`caution`、其余 `safe` |
| `:660-673` | `def should_allow_install(result: ScanResult, force: bool = False) -> Tuple[bool, str]:` | 判决函数全文：查表（`:663`）+ `hard_block`（`:665`）+ `force` 分支（`:668`）+ 三档文案（`:667`/`:669`/`:673`） |

**`agent/skill_utils.py`（811 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:148-153` | `# An ``environments:`` tag is a *relevance* gate for offer surfaces (index,` | 第三道门 `environments:`（kanban/docker/s6）：**相关性**门、fail open、显式加载永绕过——文档完全没提（见附录 C ③） |
| `:273-275` | `# Never disableable: hermes-agent is the agent's own operating manual` | `ESSENTIAL_SKILLS = frozenset({"hermes-agent"})`——`hermes-agent` 永不可禁用 |
| `:519-531` | `# ── Project skill quarantine (scan-time injection defense) ──` | 项目技能**每次读都扫**的完整理由（`git pull` 注入、编号 `#48974`、缓存落在 `HERMES_HOME` 而非仓库内）；`_PROJECT_SCAN_SOURCE = "project-local"`（`:529`） |
| `:533-557` | `def is_quarantined_project_skill(skill_md) -> bool:` | fail-closed：扫描器 import 失败 / 抛异常 → 照样隔离（`:556-557`） |
| `:559-563` | `def iter_project_skill_files(project_dir: Path):` | 项目技能的**唯一**迭代闸口（隔离决定只在这一处落地，各消费面不各自再判） |
| `:633-635` | `# ``session_platforms`` is the gateway-channel gate: session platforms the skill` | `_CONDITION_KEYS` 五键（含 `session_platforms`，文档漏列） |

**`hermes_cli/skills_hub.py`（1522 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:628-641` | `def _scan_quarantined(c: Console, q_path: Path, bundle, meta, identifier: str):` | 安装路径：**先隔离再扫**；`scan_source` 对 `official` 特判（`:634-635`）；打印扫描 provenance（fresh/cached + scanner 版本 + hash） |
| `:721-723` | `result = _scan_quarantined(c, q_path, bundle, meta, identifier)` | 安装放行闸口：`allowed, _reason = should_allow_install(result, force=force)`——`--force` 的唯一入口 |
| `:905` / `:927` | `def do_audit(name: Optional[str] = None, console: ...)` / `c.print(format_scan_report(scan_skill(...)))` | 已装技能复查（`hermes skills audit`）：**扫描沿用安装时登记的来源标识**（`:927` 用 `entry.get("identifier", entry["source"])`） |
| `:1143-1168` | `def do_publish(skill_path: str, target: str = "github", repo: str = "",` | 发布前扫描：`result = scan_skill(path, source="self")`（`:1164`）、`dangerous` 逐字 `"[bold red]Cannot publish a skill with DANGEROUS verdict.[/]"`（`:1167-1168`） |

**`tools/skill_manager_tool.py`（987 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:41-46` | `def _guard_agent_created_enabled() -> bool:` | agent 自建技能的扫描是 **opt-in**：docstring 逐字 *"skills.guard_agent_created (default False): opt-in — terminal() runs the same code ungated."* |
| `:50-72` | `def _security_scan_skill(skill_dir: Path) -> Optional[str]:` | 写后扫描：`scan_skill(skill_dir, source="agent-created")` + `should_allow_install`；`allowed is None`（即 `"ask"`）当**错误**回给 agent 让它删掉惹事内容重试 |

**`hermes_cli/subcommands/skills.py`（177 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:62` | `_flag(skills_install, "--force", help="Install despite blocked scan verdict")` | `--force` 的 CLI 面（`help` 文本自带"despite **blocked** scan verdict"的边界暗示） |

**`tools/skills_hub_models.py`（327 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:113` | `TRUST_RANK = {"builtin": 2, "trusted": 1, "community": 0}` | 信任排序表**只有三档**（`agent-created` 不参与 hub 排序）——附录 C 补 ① 的旁证：`official` 不在其中 |

**`hermes_cli/config_defaults.py`（2922 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:1372-1374` | `# code via terminal() ungated, so it mostly blocks prose with risky keywords. On: a` | agent 自建扫描的**默认值 = 关**（`:1374` 逐字 `"guard_agent_created": False,`），注释说明开启后的语义与"hub 安装永远扫"的对比 |

### mini 侧（锚 `02c61d5`）

**`agent/skill_utils.py`（409 行；`wc -l` 实测）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:1-44` | `"""技能发现与索引（对应 Hermes 的 `agent/skill_utils.py`，真实版 811 行）。` | 模块 docstring 的「抄了什么／没抄什么」两块；`:11` 逐字 *"安全扫描与四档信任（技能都是本地手写）"*——§十 ③ 的直接出处 |
| `:62-69` | `SKILL_FILENAME = "SKILL.md"` / `SKILL_PROMPT_DESC_LIMIT = 60` | 描述硬截 60，且注释里注明出处 = 真实侧 `agent/skill_utils.py:741` |
| `:144-154` | `def skills_dir() -> Path:` | 技能库根 = `get_mini_home() / "skills"`（**用户根**，不硬编码；内置根见 `:156-169`）——每个 profile 一套 |
| `:133-138` | `def iter_skill_dirs(root: Optional[Path] = None) -> List[Path]:` | **排序后返回**（索引字节稳定的前提）；docstring 直接引真实侧 `yield from map(Path, sorted(matches))` |
| `:225-230` | `def is_valid_skill_name(name: str) -> bool:` | **安全边界**（不是风格）：拒空/空白/`..`/`/`/`\`/NUL/前导点——§十 ③ 里"不抄扫描器也抄到的那一半" |
| `:288-298` | `def discover_skills(root: Optional[Path] = None) -> SkillScan:` | 每次调用**真读盘**（无缓存，"等真慢了再补"）；同名技能**目录名靠前的胜出**（对应真实侧"首见胜出"） |
| `:360-369` | `def truncate_description(description: str) -> str:` | 截断算法照真实 `:750-754`（`desc[:57] + "..."`，含省略号共 60 字符） |
| `:403-409` | `def has_skills(root: Optional[Path] = None) -> bool:` | 索引与 `skill_view` 可见性**共用的同一事实源**（两个面同开同关） |

**`tools/skills.py`（136 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:49-62` | `@no_cache_check_fn` / `def check_skills_available() -> bool:` | 工具可见性探针：用 `no_cache_check_fn`（只读本地目录、要立刻生效）；注释说明为何与索引必须同开同关 |
| `:64-85` | `@register_tool(` | `skill_view` 注册：`toolset="skills"`（`:81`）+ `check_fn=check_skills_available`（`:82`）；参数只有 `name` |
| `:87-110` | `三条失败路径都返回**可读的 JSON 错误**，一条异常都不抛：` | 三条失败路径（非法名 / 名字不存在且**回列可用名单** / 正文读不出）——"名字当路径"这条边界的落地测试对象 |
| `:44-46` | `#: 目录列表的条数上限` / `MAX_SKILL_LIST_ENTRIES = 200` | 工具结果不被超大目录撑爆（与 `tools/file_tools.list_dir` 同一理由） |

**`toolsets.py`（207 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:70-78` | `# 第 13 章新增：技能库的**第二跳**（第一跳是系统提示词里的技能索引）。` | `TOOLSETS["skills"]`（`:74`）= `{"tools": ["skill_view"]}`（`:76`）；注释解释"一个工具占一个 toolset"为何不浪费 |
| `:99-101` | `MINI_CORE_TOOLS: List[str] = [` | 默认捆含 `skill_view`（`:101`）；上面 `:92-98` 的注释把"提示词无条件宣传它 ⇒ 必须同开同关"的理由写全（第 4 章那个 bug 的形状） |

**`agent/system_prompt.py`（432 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:195-200` | `def _skill_index_parts(agent: Any) -> List[str]:` | volatile 层首块的构造入口；docstring 逐字引真实侧 `agent/system_prompt.py:705-707` 的理由（运行期可变 ⇒ 放 volatile 层最前） |
| `:302-308` | `# ── volatile 层（重建时最可能变，刻意放最后）──` / `volatile_parts: List[str] = [` | 接缝点：`*_skill_index_parts(agent)` 排在列表头（`:306`），`_memory_parts` 之前 |

**`agent/prompt_builder.py`（928 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:640-650` | `# 技能索引（第 13 章）` / `def load_skill_index() -> str:` | **内容构造**住这一层（与 `load_memory_block` 并列）、**排序**住 `system_prompt.py`——第 3 章定下的分工 |

**`hermes_cli/profiles.py`（253 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `:39-43` | `#   skills/ ← agent/skill_utils.skills_dir()（第 13 章：发现 + 提示词索引 + skill_view）` | `PROFILE_DIRS = ["logs", "hooks", "skills"]`（`:43`）——技能库从创建 profile 那一刻起就有；注释解释"在此之前没有消费者所以刻意不建" |

**`tests/test_skills.py`（691 行）/ `demos/ch13_real_run.py`（166 行）**

| 行号 | 该行/该段首 20 字 | 它证明什么 |
|---|---|---|
| `mini-hermes/tests/test_skills.py:5-14` | `## 测的是什么（6 组）` | 六组各守一条承诺的表；其中**组 5「`skill_view` 安全」**钉住的是 §十 ③ 说的那半边（越界名 / 符号链接目录 / 读不到库外） |
| `mini-hermes/tests/test_skills.py:468` | `GROUPS = [` | 六组注册表（函数定义都在它之前，"定义了 `test_` 却没登记进 `GROUPS`"会以退出码 2 报错） |
| `mini-hermes/demos/ch13_real_run.py:127-132` | `record("非法名（`../evil`）⇒ 可读错误、不抛异常",` | 真跑里与"安全边界"相关的三条：非法名 / 不存在（回列可用名单）/ 两种失败都不返回正文 |

---

## 附录 B · 本篇提到的文件与实测行数

行数一律实测：**真实侧** `git -C ~/.hermes/hermes-agent show f5d1926110:<路径> | wc -l`；**mini 侧** `wc -l mini-hermes/<路径>`。锚：真实侧 `f5d1926110`、mini 侧 `02c61d5`（mini 源码最后一次改动）。

**B-1 · 真实侧（锚 `f5d1926110`）**

| 文件 | 行数 | 它是什么 |
|---|---|---|
| `tools/skills_guard.py` | **811** | §十 主角：正则静态扫描器 + 信任感知安装策略（`INSTALL_POLICY:26`、`should_allow_install:660`、`_resolve_trust_level:780`） |
| `agent/skill_utils.py` | **811** | 技能发现/平台与条件判定/项目技能隔离扫描（`environments:` 门 `:148-153`、项目隔离 `:533-557`） |
| `hermes_cli/skills_hub.py` | **1522** | hub 安装/审计/发布三处的扫描时机（`_scan_quarantined:628`、`do_audit:905`、`do_publish:1143`） |
| `tools/skill_manager_tool.py` | **987** | agent 自建技能的写后扫描（opt-in）：`_guard_agent_created_enabled:41`、`_security_scan_skill:50` |
| `hermes_cli/config_defaults.py` | **2922** | `skills.guard_agent_created` 默认值（`:1374` `False`）藏在这里 |
| `hermes_cli/subcommands/skills.py` | **177** | `--force` 的 CLI 声明（`:62`） |
| `tools/skills_hub_models.py` | **327** | `TRUST_RANK`（`:113`，三档）——信任级别只有三档的旁证 |
| `tools/skills_tool.py` | **743** | 技能加载面（`skill_view:571`、`skills_list:232`、`skills.disabled` 硬拒绝 `:605-606`） |
| `agent/prompt_builder.py` | **1745** | 条件激活判定 `_skill_should_show:1210`（含 `session_platforms` 分支） |
| `agent/system_prompt.py` | **817** | 索引进 volatile band 首行（`:705-707`）的宿主文件 |
| `website/docs/developer-guide/creating-skills.md` | **438** | 上游原文（§十 的对照面；其「四类」「trusted 只列三仓」见附录 C） |

**B-2 · mini 侧（锚 `4b0c96a`，即第 13b 之后）**

| 文件 | 行数 | 它是什么 |
|---|---|---|
| `agent/skill_utils.py` | **551** | （本节新增）技能发现 + 索引构造的唯一实现；模块头 `:11` 明写"安全扫描不抄，技能都是本地手写"。**审计后小修**：多行块标记丢弃 + 软链 `SKILL.md` 拒收（409 → 436；13b 加多根 → 551） |
| `tools/skills.py` | **136** | （本节新增）`skill_view` 工具 + 可见性探针 + 名字安全边界 |
| `tests/test_skills.py` | **691** | （本节新增）七组契约/不变量（组 7 = 内置/用户两级共存）；组 5 覆盖名字/路径安全边界 |
| `demos/ch13_real_run.py` | **166** | 真跑脚本（17/17）：索引字节稳定 + 正文不进提示词 + `skill_view` 第二跳 + 三种边界 |
| `demos/ch13b_real_run.py` | **155** | （第 13b）两级共存的真跑（17/17）：只靠内置根也能进索引 + `skill_view`；用户同名覆盖内置；顺序确定 |
| `skills/`（仓内） | **68** | （第 13b）**内置技能根**：`README.md`（9 行）+ `mini-repo-conventions/SKILL.md`（59 行）—— 随程序发布的内容，升级时可能整体替换 |
| `tests/test_tools_runtime.py` | **929** | （第 13b）"默认工具表"断言随"有内置技能 ⇒ `skill_view` 可见"同步：一并把内置根隔离掉 |
| `toolsets.py` | **207** | `TOOLSETS["skills"]:74`（`"tools": ["skill_view"]:76`）、默认捆加 `skill_view:101` |
| `agent/system_prompt.py` | **432** | `_skill_index_parts:195`、`volatile_parts:306` |
| `agent/prompt_builder.py` | **928** | `load_skill_index:645`（内容构造层） |
| `hermes_cli/profiles.py` | **253** | `PROFILE_DIRS:43` 加 `"skills"` |
| `tests/test_gateway.py` | **1698** | `:707` 那条硬编码目录集合断言随 `PROFILE_DIRS` 同步 |
| `README.md` | **608** | 第 13 章状态行（`:40`）与 13b 行（`:42`）：精译 `b37f5db` · 实现 `02c61d5` · 内置技能级 `4b0c96a` · 全套件 **1,457/1,453** · 反验 6 条 · 真跑 17/17 |

---

## 附录 C · 上游文档与源码的出入

> 核对锚：上游 commit `f5d1926110`（v0.21.3，与《中文精译-13》同一锚）。下面**三条**均在本轮 `git show f5d1926110:<path>` 真身打印确认行号与文字相符；每条给「文档怎么说 → 源码实际怎么（`文件:行号` + 逐字）→ 影响」。末尾补四条较小的同类出入。

**① 文档说扫描器查"四类"，真实 `Finding.category` 有 13 类。**

- **文档怎么说**：§10 逐字（`creating-skills.md:420-425`）——
  ```
  420: All hub-installed skills go through a security scanner that checks for:
  421: 
  422: - Data exfiltration patterns
  423: - Prompt injection attempts
  424: - Destructive commands
  425: - Shell injection
  ```
  四条：数据外泄 / 提示词注入 / 破坏性命令 / shell 注入。
- **源码实际怎么**：`tools/skills_guard.py:42` 的 `Finding.category` 注释（逐字）是
  ```
  42:     category: str       # "exfiltration" | "injection" | "destructive" | "persistence" | "network" | ...
  ```
  注释里已经带省略号地列了五个值。**实测计数**（本轮解析 `THREAT_PATTERNS` `:114-390` 与结构检查 `:690-736`）：正则模式层 **131** 条、**12** 个不同 `category`，结构层再加 `structural` 一类，**合计 13 类**——`exfiltration(26)` / `persistence(20)` / `injection(19)` / `obfuscation(14)` / `supply_chain(10)` / `network(9)` / `destructive(8)` / `credential_exposure(7)` / `execution(6)` / `traversal(7，含结构层 2 条符号链接)` / `privilege_escalation(5)` / `mining(2)` / `structural(5)`。文档提到的四类里，"shell 注入"甚至**不是一个 category**（shell 相关的命中散在 `execution` / `obfuscation` / `supply_chain` 下），而 `persistence`（`:214`）、`obfuscation`（`:252`）、`supply_chain`（`:292`）、`credential_exposure`（`:353`）这些**高危类别文档一个字没提**。
- **影响**：文档给的是"扫描器在看什么"的**四类直觉**，不是类别清单；照它去核对扫描覆盖面会**漏掉八类**，其中 `credential_exposure`（硬编码密钥/GitHub token，`critical`）与 `persistence`（写 agent 启动配置）都是会直接决定 `dangerous` 裁决的类别。读文档得把它当"举例"，权威清单在代码里。

**② 文档只提 community 的 `--force` 边界，真实的 `trusted` 的 `dangerous` 也 block。**

- **文档怎么说**：§10 逐字（`creating-skills.md:431`）——
  ```
  431: - `community` — non-dangerous findings can be overridden with `--force`; `dangerous` verdicts remain blocked
  ```
  以及 `:430` 只给了 `trusted` 的来源，没给它的边界：
  ```
  430: - `trusted` — from openai/skills, anthropics/skills, huggingface/skills
  ```
  合起来读，读者很容易得出"`trusted` 比 `community` 宽松，`--force` 之外的差异不重要"。
- **源码实际怎么**：策略矩阵 `tools/skills_guard.py:29` 逐字 `"trusted": ("allow", "allow", "block"),`——**`trusted` 的 `dangerous` 是 `"block"`，与 `community`（`:30`）完全一样**。更关键的是 `--force` 的覆盖范围被一句硬边界限死：`:665` 逐字
  ```
  665:     hard_block = result.verdict == "dangerous" and result.trust_level in ("community", "trusted")
  ```
  `:668` 逐字 `if force and not hard_block:`——**`trusted` + `dangerous` 时 `--force` 与在 `community` 上一样无效**，返回理由（`:673`）逐字 `"--force does not override a dangerous verdict."`。docstring（`:661-662`）也把两档写在一起：*"*force* overrides every block **except a dangerous verdict on community/trusted sources**."*
- **影响**：把 `trusted` 读成"除了来源不同、行为更宽松"会**低估它被拦的概率**——`trusted` 仓库里的一个 critical 发现（比如硬编码私有密钥）同样装不进去，`--force` 也救不了。文档的 `trusted` 一句是**不完整的**：它给了来源与"caution allowed"，没给"dangerous blocked"。

**③ 文档完全没提第三道 `environments:` 相关性门。**

- **文档怎么说**：§3.2「条件激活」只列了四个 `requires_*` / `fallback_for_*` 字段（`creating-skills.md:126-131` 的表格，示例 `:120-123`），`platforms:` 另立一节（`:101-111`）。全篇 grep `environment` 只命中 `required_environment_variables` 与环境变量那一节（`:137-181`）——**没有一处 `environments:`**。
- **源码实际怎么**：`agent/skill_utils.py:148-153` 逐字——
  ```
  148: # An ``environments:`` tag is a *relevance* gate for offer surfaces (index,
  149: # autocomplete, slash commands), not a compatibility gate: an explicit load
  150: # (skill_view, --skills) always succeeds. Detection is cached per process.
  ```
  即除了"条件激活"四键与"平台兼容"（`platforms:`）之外，还有**第三道门**：`environments:`（标签如 kanban/docker/s6）。它与前两门语义都不同——**是"相关性"门不是"兼容性"门**，只影响**offer 面**（索引 / 自动补全 / 斜杠命令），**显式加载永远绕过**，且探测结果按进程缓存（`_ENV_DETECT_CACHE` `:152`）。
- **影响**：这是**漏列**（不是矛盾）。写 `SKILL.md` 的人若不知道这道门，会以为"不进索引 = 技能没被认出来"，而实际可能是被 `environments:` 相关性地挡在 offer 面之外；反过来，调试"为什么加载得到、补全里看不到"时，**只在 `platforms:` 与四键里找原因是找不到的**。

**补 · 另四条较小的同类出入（同锚，逐条给 `文件:行号`）**

1. **`official` 不是信任级别，而是来源（provenance）；它的信任级别是 `builtin`。** 文档 §10（`creating-skills.md:428-431`）逐字把 `official` 与其余三档**并列在 "Trust levels:" 标题下**。源码里策略矩阵的键（`tools/skills_guard.py:26-33`）只有 `builtin` / `trusted` / `community` / `agent-created` 四个，**没有 `official`**；`official` 是**来源 id**，在 `_resolve_trust_level` 里被翻译成 `builtin`——`:786-787` 逐字 `if src == "official":` / `return "builtin"`，docstring（`:781`）逐字 *"\"official\" is provenance, not a user-controlled GitHub id like \"official/<repo>\""*。信任排序表也印证级别只有三档：`tools/skills_hub_models.py:113` 逐字 `TRUST_RANK = {"builtin": 2, "trusted": 1, "community": 0}`（无 `official`）。**影响**：术语层面**对不上**——文档的"四档信任"实为"三档信任 + 一个来源 id"。
2. **`trusted` 仓库清单少列一个：`NVIDIA/skills`。** 文档 `:430` 逐字 *"`trusted` — from openai/skills, anthropics/skills, huggingface/skills"*（三个）。源码 `tools/skills_guard.py:24` 逐字 `TRUSTED_REPOS = {"openai/skills", "anthropics/skills", "huggingface/skills", "NVIDIA/skills"}`（四个），`:23` 注释逐字 *"NVIDIA-verified skills each ship a signed `skill.oms.sig` + governance `skill-card.md`."*。**影响**：清单**少列一项**。
3. **条件激活字段少列一个：`session_platforms`。** 文档 §3.2 只列四键（`:128-131`）。源码 `agent/skill_utils.py:635` 逐字 `_CONDITION_KEYS = ("fallback_for_toolsets", "requires_toolsets", "fallback_for_tools", "requires_tools", "session_platforms")`——**五键**；`:633-634` 注释逐字 *"``session_platforms`` is the gateway-channel gate: session platforms the skill is FOR (hidden from the index elsewhere), unlike ``platforms:`` (host OS)."*。**影响**：**漏列**——第五个条件字段是网关**渠道**闸门，与 `platforms:`（宿主 OS）不是一回事。
4. **第四档信任级别 `agent-created` 未在文档中提及。** 源码 `tools/skills_guard.py:31-32` 逐字 `# "ask" = error to the agent (retry without the flagged content); only when skills.guard_agent_created is on.` / `"agent-created": ("allow",  "allow",   "ask"),`；`:784-785` 逐字 `if src == "agent-created":` / `return "agent-created"`。这一档的 `dangerous` 判决是 **`"ask"`**（第三种判决值，既非 allow 也非 block），且**默认关闭**（`hermes_cli/config_defaults.py:1374` 逐字 `"guard_agent_created": False,`）。**影响**：文档的"信任级别"清单**不完整**，且漏掉的是唯一一档"判决值不是 allow/block"的。
