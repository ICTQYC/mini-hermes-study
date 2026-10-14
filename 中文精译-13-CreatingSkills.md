# 中文精译 · 第 13 章：创建技能（Creating Skills）

> **本篇定位**：精译为主（看懂"什么时候该写技能而不是工具、`SKILL.md` 怎么写、技能该放哪里、以及怎么发布与安装"）。它是《代码归属图》（精译-12 上篇）里 `Skills（bundled & optional）` 那一行的展开页；差异对比、源码深挖与实现日志留给《源码讲解-13》。
> **来源**：`官方文档-Developer-Guide/creating-skills.md`（**438 行** · H1 + 10 个 H2 + 12 个 H3 + 2 个 H4；`title` 逐字：*Creating Skills*）。
> **上游**：commit `f5d1926110` · `version = "0.21.3"` · 原文 **438 行**

> 术语说明：secret 指密钥、令牌等机密值，并不表示它已加密；dotpath 指用点分隔的配置路径，如 `skills.config.myplugin.path`；tap 指用户登记的技能源仓库。

[TOC]

---

## 0. 这一章在回答什么

技能是给 Hermes Agent 添加新能力的**首选方式**。它们比工具更容易创建，不需要对 agent 做任何代码修改，并且可以跟社区共享。这一章回答**一个能力从"该不该做成技能"到"发布出去、被人装上"的完整生命周期**：

- **第 1–3 节**给出判据与格式：什么时候该写成技能，技能住在 `skills/`（随包内置）还是 `optional-skills/`（官方可选），`SKILL.md` 的 frontmatter（首部元数据）能声明什么 —— `platforms`、四个 **条件激活**字段、环境变量与凭据文件需求 —— 以及正文该有的五个小节（When to Use / Quick Reference / Procedure / Pitfalls / Verification）。
- **第 4 节**讲**加载时的安全设置**：缺密钥**不**把技能从"可发现"里藏起来，而是在本地 CLI 里安全征询；原始机密值**永不**暴露给模型；非敏感的设置在 `config.yaml` 的 `skills.config` 命名空间下，OAuth 令牌这类**文件形态**的凭据另有声明方式。
- **第 5 节**是编写准则：零外部依赖、**渐进披露**、带上辅助脚本、用 `[[as_document]]` **以文档形式投递媒体**、`${HERMES_SKILL_DIR}` 模板替换，以及**显式开启（opt-in）**的内联 shell 片段。
- **第 6–8 节**讲归属与自动化：技能该住在内置、`optional-skills/` 还是**技能中心（Skills Hub）**；**蓝图（blueprint）** —— 一个额外声明了排程的普通技能，装进来只登记成**待办建议**、绝不自动排程；以及统一的**建议的定时任务**面。
- **第 9–10 节**讲发布与安全扫描：发到技能中心或自建 tap，四档信任级别与 `dangerous` 裁决的硬拦截，以及三种第三方发现模型。

---

## 1. Should it be a Skill or a Tool? —— 该做成技能还是工具？

做成**技能**，当：

- 这项能力可以表达成**指令 + shell 命令 + 现有工具**；
- 它包装的是一个 agent 能通过 `terminal` 或 `web_extract` 调用的外部 CLI 或 API；
- 它不需要把定制的 Python 集成或 API 密钥管理烤进 agent；
- 例子：arXiv 搜索、git 工作流、Docker 管理、PDF 处理、经由 CLI 工具的邮件。

做成**工具**，当：

- 它需要与 API 密钥、鉴权流程或多组件配置做端到端集成；
- 它需要一套每次都必须精确执行的定制处理逻辑；
- 它处理二进制数据、流式数据或实时事件；
- 例子：浏览器自动化、TTS、视觉分析。

---

## 2. Skill Directory Structure —— 技能目录结构

随包内置的技能都住在 `skills/` 里，按类别组织。官方可选技能在 `optional-skills/` 里用**同一套结构**：

```text
skills/
├── research/
│   └── arxiv/
│       ├── SKILL.md              # Required: main instructions
│       └── scripts/              # Optional: helper scripts
│           └── search_arxiv.py
├── productivity/
│   └── ocr-and-documents/
│       ├── SKILL.md
│       ├── scripts/
│       └── references/
└── ...
```

---

## 3. SKILL.md Format —— SKILL.md 格式

```markdown
---
name: my-skill
description: Brief description (shown in skill search results)
version: 1.0.0
author: Your Name
license: MIT
platforms: [macos, linux]          # Optional — restrict to specific OS platforms
                                   #   Valid: macos, linux, windows
                                   #   Omit to load on all platforms (default)
metadata:
  hermes:
    tags: [Category, Subcategory, Keywords]
    related_skills: [other-skill-name]
    requires_toolsets: [web]            # Optional — only show when these toolsets are active
    requires_tools: [web_search]        # Optional — only show when these tools are available
    fallback_for_toolsets: [browser]    # Optional — hide when these toolsets are active
    fallback_for_tools: [browser_navigate]  # Optional — hide when these tools exist
    config:                              # Optional — config.yaml settings the skill needs
      - key: my.setting
        description: "What this setting controls"
        default: "sensible-default"
        prompt: "Display prompt for setup"
    blueprint:                              # Optional — marks this skill a runnable automation
      schedule: "0 9 * * *"              #   cron expr / "every 2h" / ISO timestamp
      deliver: origin                    #   optional (default origin)
      prompt: "Task instruction for each run"  # optional
      no_agent: false                    # optional
required_environment_variables:          # Optional — env vars the skill needs
  - name: MY_API_KEY
    prompt: "Enter your API key"
    help: "Get one at https://example.com"
    required_for: "API access"
---

# Skill Title

Brief intro.

## When to Use
Trigger conditions — when should the agent load this skill?

## Quick Reference
Table of common commands or API calls.

## Procedure
Step-by-step instructions the agent follows.

## Pitfalls
Known failure modes and how to handle them.

## Verification
How the agent confirms it worked.
```

### 平台专属技能（Platform-Specific Skills）

技能可以用 `platforms` 字段把自己限定到特定操作系统：

```yaml
platforms: [macos]            # macOS only (e.g., iMessage, Apple Reminders)
platforms: [macos, linux]     # macOS and Linux
platforms: [windows]          # Windows only
```

设置之后，在不兼容的平台上，该技能会自动从系统提示词、`skills_list()` 和斜杠命令里隐藏。若省略或为空，技能在**所有**平台上加载（向后兼容）。

### 条件激活（Conditional Skill Activation）

技能可以声明对特定工具或工具集的依赖。这控制该技能在给定会话里是否出现在系统提示词中。

```yaml
metadata:
  hermes:
    requires_toolsets: [web]           # Hide if the web toolset is NOT active
    requires_tools: [web_search]       # Hide if web_search tool is NOT available
    fallback_for_toolsets: [browser]   # Hide if the browser toolset IS active
    fallback_for_tools: [browser_navigate]  # Hide if browser_navigate IS available
```

| 字段 | 行为 |
|-------|----------|
| `requires_toolsets` | 当所列工具集中**任一个**不可用时，技能被**隐藏** |
| `requires_tools` | 当所列工具中**任一个**不可用时，技能被**隐藏** |
| `fallback_for_toolsets` | 当所列工具集中**任一个**可用时，技能被**隐藏** |
| `fallback_for_tools` | 当所列工具中**任一个**可用时，技能被**隐藏** |

**`fallback_for_*` 的用例：** 造一个"主工具不在时充当替代方案"的技能。例如一个 `duckduckgo-search` 技能带上 `fallback_for_tools: [web_search]`，就只在 web 搜索工具（它需要 API 密钥）未配置时才出现。

**`requires_*` 的用例：** 造一个"只有某些工具在场时才说得通"的技能。例如一个网页抓取工作流技能带上 `requires_toolsets: [web]`，在 web 工具被禁用时就不会去把提示词搅乱。

### 环境变量需求（Environment Variable Requirements）

技能可以声明自己需要的环境变量。当技能经由 `skill_view` 加载时，它声明的必需变量会被自动登记，以便透传进沙箱化的执行环境（`terminal`、`execute_code`）。

```yaml
required_environment_variables:
  - name: TENOR_API_KEY
    prompt: "Tenor API key"               # Shown when prompting user
    help: "Get your key at https://tenor.com"  # Help text or URL
    required_for: "GIF search functionality"   # What needs this var
```

每一项支持：

- `name`（必需）—— 环境变量名
- `prompt`（可选）—— 向用户索要该值时的提示文本
- `help`（可选）—— 帮助文本，或获取该值的 URL
- `required_for`（可选）—— 描述哪个功能需要这个变量

用户也可以在 `config.yaml` 里手工配置透传变量：

```yaml
terminal:
  env_passthrough:
    - MY_CUSTOM_VAR
    - ANOTHER_VAR
```

macOS 专属技能的例子见 `skills/apple/`。

---

## 4. Secure Setup on Load —— 加载时的安全设置

当技能需要 API 密钥或令牌时，用 `required_environment_variables`。缺值**不会**把技能从发现里隐藏。取而代之的是：在本地 CLI 里加载该技能时，Hermes 会安全地征询它们。

```yaml
required_environment_variables:
  - name: TENOR_API_KEY
    prompt: Tenor API key
    help: Get a key from https://developers.google.com/tenor
    required_for: full functionality
```

用户可以跳过设置，照常继续加载该技能。Hermes **永不**把原始机密值暴露给模型。网关与消息会话会展示本地设置指引，而不是在带内收集机密值。

> 💡 **提示 · 沙箱透传（Sandbox Passthrough）**
> 技能被加载时，任何**已设置**的、声明过的 `required_environment_variables` 都会被**自动透传**给 `execute_code` 与 `terminal` 沙箱 —— 包括 Docker、Modal 这类远端后端。你的技能脚本可以用 `$TENOR_API_KEY`（Python 里是 `os.environ["TENOR_API_KEY"]`），用户无需再做任何额外配置。细节见[环境变量透传](https://hermes-agent.nousresearch.com/docs/user-guide/security#environment-variable-passthrough)（Environment Variable Passthrough）。

旧的 `prerequisites.env_vars` 作为向后兼容别名，仍然受支持。

### 配置项设置（Config Settings (config.yaml)）

技能可以声明非敏感的设置，它们存放在 `config.yaml` 的 `skills.config` 命名空间下。与环境变量（机密值，存在 `.env` 里）不同，config 设置面向路径、偏好以及其它非敏感值。

```yaml
metadata:
  hermes:
    config:
      - key: myplugin.path
        description: Path to the plugin data directory
        default: "~/myplugin-data"
        prompt: Plugin data directory path
      - key: myplugin.domain
        description: Domain the plugin operates on
        default: ""
        prompt: Plugin domain (e.g., AI/ML research)
```

每一项支持：

- `key`（必需）—— 该设置的 dotpath（例如 `myplugin.path`）
- `description`（必需）—— 说明该设置控制什么
- `default`（可选）—— 用户未配置时的默认值
- `prompt`（可选）—— `hermes config migrate` 期间展示的提示文本；缺省回退到 `description`

**它如何工作：**

1. **存储：** 值被写进 `config.yaml` 的 `skills.config.<key>` 下：

   ```yaml
   skills:
     config:
       myplugin:
         path: ~/my-data
   ```

2. **发现：** `hermes config migrate` 会扫描所有已启用的技能，找出未配置的设置并提示用户。这些设置也会出现在 `hermes config show` 的 "Skill Settings" 下。

3. **运行时注入：** 技能加载时，它的 config 值会被解析并追加到技能消息里：

   ```
   [Skill config (from ~/.hermes/config.yaml):
     myplugin.path = /home/user/my-data
   ]
   ```

   agent 于是看到已配置的值，而无需自己去读 `config.yaml`。

4. **手工设置：** 用户也可以直接设值：

   ```bash
   hermes config set skills.config.myplugin.path ~/my-data
   ```

> 💡 **提示 · 何时用哪个（When to use which）**
> API 密钥、令牌以及其它**机密值**用 `required_environment_variables`（存在 `~/.hermes/.env`，永不展示给模型）。**路径、偏好和非敏感设置**用 `config`（存在 `config.yaml`，在 config show 里可见）。

### 凭据文件需求（Credential File Requirements (OAuth tokens, etc.)）

使用 OAuth 或基于文件的凭据的技能，可以声明哪些文件需要被挂载进远端沙箱。这针对的是以**文件**形式（而非环境变量）存放的凭据 —— 典型是安装脚本产出的 OAuth 令牌文件。

```yaml
required_credential_files:
  - path: google_token.json
    description: Google OAuth2 token (created by setup script)
  - path: google_client_secret.json
    description: Google OAuth2 client credentials
```

每一项支持：

- `path`（必需）—— 相对 `~/.hermes/` 的文件路径
- `description`（可选）—— 说明该文件是什么、如何产生

加载时，Hermes 会检查这些文件是否存在。缺失的文件会触发 `setup_needed`。存在的文件会被自动：

- 以**只读绑定挂载**的方式**挂载进 Docker** 容器
- **同步进 Modal** 沙箱（创建时 + 每条命令之前，因此会话进行中的 OAuth 也能工作）
- 在**本地**后端无需任何特殊处理即可用

> 💡 **提示 · 何时用哪个（When to use which）**
> 简单的 API 密钥与令牌（存在 `~/.hermes/.env` 里的字符串）用 `required_environment_variables`。OAuth 令牌文件、client secret、服务账号 JSON、证书，或任何以磁盘文件形式存在的凭据，用 `required_credential_files`。

两者同时使用的完整例子见 `skills/productivity/google-workspace/SKILL.md`。

---

## 5. Skill Guidelines —— 技能编写准则

### 不引入外部依赖（No External Dependencies）

优先用 stdlib Python、curl 以及现有的 Hermes 工具（`web_extract`、`terminal`、`read_file`）。如果确实需要一个依赖，把安装步骤写进技能里。

### 渐进披露（Progressive Disclosure）

把最常用的工作流放在最前面。边界情况与进阶用法放在底部。这样常见任务上的 token 消耗会保持很低。

### 带上辅助脚本（Include Helper Scripts）

对于 XML/JSON 解析或复杂逻辑，把辅助脚本放进 `scripts/` —— 不要指望 LLM 每次都现场写解析器。

### 以文档形式投递媒体（Deliver media as documents · `[[as_document]]`）

如果你的技能会产出高分辨率截图、图表，或任何"有损预览压缩会造成伤害"的图像 —— 在响应的某处（通常是最后一行）发出字面指令 `[[as_document]]`。网关会剥掉这条指令，并把该次响应里抽取到的每一个媒体路径**作为可下载的文件附件**投递，而不是内联的图片气泡。完整语义见[技能输出与媒体投递](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills#skill-output-and-media-delivery)（Skill output and media delivery）。

#### 从 SKILL.md 引用随包脚本（Referencing bundled scripts from SKILL.md）

技能被加载时，激活消息会把技能的绝对目录暴露为 `[Skill directory: /abs/path]`，并在 SKILL.md 正文的**任意位置**替换两个模板 token：

| Token | 替换为 |
|---|---|
| `${HERMES_SKILL_DIR}` | 该技能目录的绝对路径 |
| `${HERMES_SESSION_ID}` | 当前活跃的会话 id（没有会话时原样保留） |

于是 SKILL.md 可以让 agent 直接运行一个随包脚本：

```markdown
To analyse the input, run:

    node ${HERMES_SKILL_DIR}/scripts/analyse.js <input>
```

agent 看到的是替换后的绝对路径，并用一条可直接运行的命令调用 `terminal` 工具 —— 不必做路径运算，也不必多跑一趟 `skill_view`。用 `config.yaml` 里的 `skills.template_vars: false` 全局关掉替换。

#### 内联 shell 片段（Inline shell snippets (opt-in)）

技能还可以在 SKILL.md 正文里嵌入写作 `` !`cmd` `` 的内联 shell 片段。启用后，每个片段的 stdout 会在 agent 读到它之前被内联进消息，于是技能可以注入动态上下文：

```markdown
Current date: !`date -u +%Y-%m-%d`
Git branch: !`git -C ${HERMES_SKILL_DIR} rev-parse --abbrev-ref HEAD`
```

这**默认关闭** —— SKILL.md 里的任何片段都会在主机上、**不经审批**地运行，所以只对**你信任的技能来源**启用：

```yaml
# config.yaml
skills:
  inline_shell: true
  inline_shell_timeout: 10   # seconds per snippet
```

片段以技能目录作为工作目录运行，输出被限制在 4000 个字符以内。失败（超时、非零退出）会以一个简短的 `[inline-shell error: ...]` 标记出现，而不会把整个技能弄坏。

### 跑一遍试试（Test It）

运行该技能，验证 agent 正确遵循了指令：

```bash
hermes chat --toolsets skills -q "Use the X skill to do Y"
```

---

## 6. Where Should the Skill Live? —— 技能该住在哪里？

随包内置的技能（在 `skills/` 里）随每一次 Hermes 安装一起发出。它们应当**对大多数用户普遍有用**：

- 文档处理、网页研究、常见开发工作流、系统管理
- 被广泛的人群定期使用

如果你的技能是官方的、有用但并非人人需要（例如某个付费服务集成、某个重量级依赖），把它放进 **`optional-skills/`** —— 它随仓库发出，可经 `hermes skills browse` 发现（标为 "official"），并以内置信任安装。

如果你的技能是专用的、社区贡献的、或小众的，它更适合放进一个**技能中心（Skills Hub）** —— 上传到某个注册表，通过 `hermes skills install` 分享。

---

## 7. Blueprints: skills that are also automations —— 蓝图：同时也是自动化的技能

**蓝图（blueprint）**是一个普通技能，只不过额外在 frontmatter 里声明了排程。加上一个 `metadata.hermes.blueprint` 块，这个技能就变成一个**可分享、可运行**的自动化：

```yaml
metadata:
  hermes:
    tags: [blueprint, email]
    blueprint:
      schedule: "0 8 * * *"     # presence of `blueprint:` marks it runnable
      deliver: telegram          # optional (default: origin)
      prompt: "Summarize my unread email and today's calendar."  # optional
      no_agent: false            # optional
```

因为蓝图**本身就是**一个技能，所以它会原封不动地流过整条技能流水线 —— 搜索、检视、安装、安全扫描、来源追溯、tap、集中式索引，以及用 `hermes skills publish` 分享。没有新东西要学。

**安装一个蓝图。** 当你安装一个带 `blueprint:` 块的技能时，Hermes 会把它登记成一条**建议的定时任务**，而不是直接排程。排程是**显式开启（opt-in）**的 —— 安装**从不**静默造出一个循环任务。你通过 `/suggestions` 审阅并接受它：

```bash
hermes skills install owner/morning-brief
# → Blueprint: 'morning-brief' is an automation (schedule 0 8 * * *).
#   Added to your suggestions — run /suggestions to schedule or dismiss it.

# then, in a session:
/suggestions             # lists pending suggestions, numbered
/suggestions accept 1    # creates the cron job
/suggestions dismiss 1   # never offer it again
```

蓝图是统一的"建议的定时任务"面里的一个**来源** —— 策展的起步自动化、以及（以后）使用模式与集成建议都出现在同一个地方。见下方[建议的定时任务](#suggested-cron-jobs)。

**分享你造出来的自动化。** 一个被定时任务加载的蓝图（`hermes cron create --skill <name> ...`）可以导出回一个 SKILL.md，并像任何其它技能一样发布，于是你为自己调好的自动化，对别人来说变成一条命令就能装上的东西。

蓝图这一层**没有**引入新的对象类型、存储或传输 —— 蓝图就是技能，排程就是定时任务，分享走的是既有的 publish/tap/index 路径。

---

<a id="suggested-cron-jobs"></a>

## 8. Suggested Cron Jobs —— 建议的定时任务

Hermes 可以**提议**自动化、让你一点即受，而不必逼你手工拼装定时任务。每一条提议都流经**同一个面** —— `/suggestions` 命令 —— 无论它来自哪里：

| 来源 | 触发条件 |
|--------|---------|
| `catalog` | 策展的起步自动化（`/suggestions catalog`）—— 每日简报、重要邮件监控、每周回顾、工作日开始提醒 |
| `blueprint` | 你安装了一个带 `blueprint:` 块的技能 |
| `usage` | 后台回顾注意到某个反复出现的诉求可以用排程来满足 |
| `integration` | 你接上了某个账号（Gmail、GitHub……），于是显然的自动化被提出来 |

```bash
/suggestions             # list pending
/suggestions accept N    # schedule suggestion N (creates the cron job)
/suggestions dismiss N   # dismiss it — latched, never re-offered
/suggestions catalog     # add the curated starter automations
```

接受一条建议调用的是 `cronjob` 工具所用的同一个 `cron.jobs.create_job` —— 没有第二套任务引擎。建议**永不**自动创建任务；接受永远是显式的。被驳回的建议按一个**稳定键**闩锁，因此同一条提议永不重提。待办列表有上限，以免它变成一堵唠叨墙。

**重要邮件监控**这条目录项就是"轮询 → 分类 → 上浮"的模式：它用一个便宜的分类器模型（`config.yaml` 里的 `auxiliary.monitor`）给收件箱条目打分，只投递超过紧急阈值的那些，其余保持沉默。

---

## 9. Publishing Skills —— 发布技能

### 发布到技能中心（To the Skills Hub）

```bash
hermes skills publish skills/my-skill --to github --repo owner/repo
```

### 发布到自定义仓库（To a Custom Repository）

把你的仓库加成某个 tap：

```bash
hermes skills tap add owner/repo
```

之后用户就能从你的仓库里搜索并安装。

---

## 10. Security Scanning —— 安全扫描

所有从 hub 安装的技能都要过一个安全扫描器，它检查：

- 数据外泄模式
- 提示词注入尝试
- 破坏性命令
- shell 注入

信任级别：

- `builtin` —— 随 Hermes 发出（永远受信任）
- `official` —— 来自仓库里的 `optional-skills/`（内置信任，无第三方警告）
- `trusted` —— 来自 openai/skills、anthropics/skills、huggingface/skills
- `community` —— 非危险的发现可以用 `--force` 覆盖；`dangerous` 裁决仍被拦截

Hermes 现在可以从多种外部发现模型消费第三方技能：

- 直接的 GitHub 标识（例如 `openai/skills/k8s`）
- `skills.sh` 标识（例如 `skills-sh/vercel-labs/json-render/json-render-react`）
- 从 `/.well-known/skills/index.json` 提供的 well-known 端点

如果你希望自己的技能无需一个 GitHub 专用的安装器就能被发现，考虑在把它发布进仓库或市场之外，另外从一个 well-known 端点提供它。

---

## 附录 A · 译名对照

| 术语 | 译名 | 首次出现节 |
|---|---|---|
| skill | 技能 | §0 |
| tool | 工具 | §1 |
| SKILL.md | `SKILL.md`（原样，不译） | §2 |
| frontmatter | frontmatter（首部元数据） | §0 |
| bundled skills | 随包内置技能 | §2 |
| optional skills | 官方可选技能 | §2 |
| category | 类别 | §2 |
| platform-specific | 平台专属 | §3.1 |
| conditional activation | 条件激活 | §3.2 |
| toolset | 工具集 | §3.2 |
| `fallback_for_*` | 回退字段（`fallback_for_*`） | §3.2 |
| `requires_*` | 依赖字段（`requires_*`） | §3.2 |
| environment variable | 环境变量 | §3.3 |
| passthrough | 透传 | §3.3 |
| sandboxed execution environment | 沙箱化的执行环境 | §3.3 |
| secure setup on load | 加载时的安全设置 | §4 |
| secret（raw secret value） | 机密值（原始机密值） | §4 |
| in-band | 带内 | §4 |
| config settings | 配置项设置 | §4.1 |
| dotpath | dotpath | §4.1 |
| runtime injection | 运行时注入 | §4.1 |
| credential file | 凭据文件 | §4.2 |
| OAuth token file | OAuth 令牌文件 | §4.2 |
| client secret | client secret（客户端密钥） | §4.2 |
| read-only bind mount | 只读绑定挂载 | §4.2 |
| `setup_needed` | `setup_needed`（原样，不译） | §4.2 |
| external dependencies | 外部依赖 | §5.1 |
| progressive disclosure | 渐进披露 | §5.2 |
| helper script | 辅助脚本 | §5.3 |
| media delivery | 媒体投递 | §5.4 |
| lossy preview compression | 有损预览压缩 | §5.4 |
| directive | 字面指令（directive） | §5.4 |
| file attachment | 文件附件 | §5.4 |
| activation message | 激活消息 | §5.4.1 |
| template token | 模板 token | §5.4.1 |
| inline shell snippet | 内联 shell 片段 | §5.4.2 |
| opt-in | 显式开启（opt-in） | §0 |
| stdout | stdout | §5.4.2 |
| blueprint | 蓝图 | §0 |
| schedule | 排程 | §7 |
| deliver | 投递 | §7 |
| suggested cron job | 建议的定时任务 | §7 |
| suggestion | 建议 | §7 |
| latched | 闩锁（latched） | §8 |
| curated | 策展的 | §8 |
| catalog | 目录（catalog） | §8 |
| usage pattern | 使用模式 | §7 |
| integration | 集成 | §7 |
| provenance | 来源追溯（provenance） | §7 |
| tap | tap | §7 |
| centralized index | 集中式索引 | §7 |
| publish | 发布 | §7 |
| Skills Hub | 技能中心（Skills Hub） | §6 |
| registry | 注册表 | §6 |
| trust level | 信任级别 | §10 |
| verdict | 裁决 | §10 |
| data exfiltration | 数据外泄 | §10 |
| prompt injection | 提示词注入 | §10 |
| destructive command | 破坏性命令 | §10 |
| shell injection | shell 注入 | §10 |
| well-known endpoint | well-known 端点 | §10 |
| gateway | 网关 | §4 |
| curator | 策展器（curator） | —（原文未出现，不作为本章译名） |

---

## 附录 B · 本篇提到的文件与实测行数

行数一律 `git show f5d1926110:<路径> | wc -l` 实测，路径先经 `git ls-tree -r f5d1926110 --name-only` 确认存在（目录按下挂文件数计）。**实测锚 `f5d1926110`。**

**B-1 · 原文按路径点名的文件与目录：**

| 路径 | 实测 | 角色 |
|---|---|---|
| `skills/` | **331 个文件**（目录） | 随包内置技能根目录（`:30-41` 的示例树的根） |
| `optional-skills/` | **788 个文件**（目录） | 官方可选技能根目录，与 `skills/` 同构 |
| `skills/apple/` | **5 个文件**（目录） | §3.3 结尾"macOS 专属技能的例子"（`apple-notes`/`apple-reminders`/`findmy`/`imessage` + `DESCRIPTION.md`） |
| `skills/productivity/google-workspace/SKILL.md` | **338 行** | §4.2 "两者同时使用的完整例子" |
| `skills/research/arxiv/SKILL.md` | **282 行** | §2 示例树里的 `research/arxiv/`（在场） |
| `skills/productivity/ocr-and-documents/` | **不在树中** | §2 示例树列出，但该锚点下不存在（见附录 C 第 4 条） |
| `config.yaml`（`~/.hermes/config.yaml`） | — | §4.1 的 config 设置命名空间、§5 的 `skills.template_vars`、§5.4.2 的 `skills.inline_shell` |
| `~/.hermes/.env` | — | §4 的机密值存放处（`.env`） |
| `skills/my-skill` | **不在树中** | §9.1 发布命令里的**占位示例路径**，非仓库实体 |
| `skills/AGENTS.md` | **78 行** | `skills/` 目录自身的约定文件（原文未点名；为目录级实测补上） |

**B-2 · 与本章各节符号对应的宿主源码文件（原文按符号/命令点名，落到锚点上的所在）：**

| 文件 | 实测 | 角色 |
|---|---|---|
| `tools/skills_tool.py` | **743 行** | `skill_view`（`:571`）、`skills_list`（`:232`）；§3.3/§4 的加载时登记与 `setup_needed`（`:420-469`） |
| `tools/skill_manager_tool.py` | **987 行** | `skill_manage`（`:794`） |
| `agent/skill_utils.py` | **811 行** | §3.1 的 `platforms` 兼容（`skill_matches_platform` `:143`）、§3.2 的条件键表 `_CONDITION_KEYS`（`:635`）、§4.1 的 `SKILL_CONFIG_PREFIX = "skills.config"`（`:693`） |
| `agent/prompt_builder.py` | **1745 行** | §3.2 的判定函数 `_skill_should_show`（`:1210`，四条 `any(...)` 分支 `:1224-1227`） |
| `agent/skill_preprocessing.py` | **103 行** | §5.4.1 的 `${HERMES_*}` 正则（`:15`）、§5.4.2 的内联 shell 正则（`:17`）与 4000 字符上限（`:19`） |
| `agent/skill_commands.py` | **656 行** | §5.4.1 的激活消息 `[Skill directory: …]`（`:248`）、§4.1 的 `[Skill config (from …)]`（`:194`） |
| `tools/skills_tool_setup.py` | **150 行** | §3.3/§4 的需求解析（`_get_required_environment_variables` `:46`）；§4 的 `prerequisites.env_vars` 兼容别名（`:48`） |
| `tools/env_passthrough.py` | **146 行** | §3.3/§4 的 `register_env_passthrough`（`:56`） |
| `tools/credential_files.py` | **366 行** | §4.2 的凭据文件注册：`register_credential_file`（`:63`）、`register_credential_files`（`:106`） |
| `tools/environments/docker.py` | **1082 行** | §4.2 的"只读绑定挂载"：`_readonly_skill_mount_args`（`:443`）、`:ro`（`:462`） |
| `hermes_cli/config.py` | **4027 行** | §4.1 的 `hermes config migrate` 扫描（`:994`）、`skills.config`（`:1530`）、`hermes config show` 的 "Skill Settings"（`:2978`） |
| `tools/blueprints.py` | **207 行** | §7 的蓝图：`parse_blueprint`（`:59`）、`blueprint_spec_for_installed`（`:99`）、`register_blueprint_suggestion`（`:140`）、`export_blueprint`（`:160`） |
| `hermes_cli/skills_hub.py` | **1522 行** | §7 的"安装蓝图只登记建议、不排程"：`_announce_blueprint`（`:562`） |
| `hermes_cli/subcommands/skills.py` | **177 行** | §9 的 `publish`（`:148`）、`tap add`（`:168`）；§6 的 `browse`（`:38`） |
| `cron/suggestions.py` | **202 行** | §8 的建议面：`MAX_PENDING = 5`（`:37`）、`VALID_SOURCES`（`:39`）、`dismiss_suggestion` 闩锁（`:162`） |
| `cron/suggestion_catalog.py` | **143 行** | §8 目录四项（`daily-briefing` `:39`、`important-mail-monitor` `:58`、`weekly-review` `:82`、`standup-reminder` `:99`） |
| `cron/blueprint_catalog.py` | **770 行** | §8 的 `cron.jobs.create_job` 无第二引擎（`:4`） |
| `cron/jobs.py` | **3445 行** | §8 的 `create_job`（`:1755`） |
| `hermes_cli/suggestions_cmd.py` | **121 行** | §8 的 `/suggestions` 命令实现（`:1`） |
| `hermes_cli/config_defaults.py` | **2922 行** | §8 的 `auxiliary.monitor`（`:741`）、§5 的 `template_vars`（`:1366`）与 `inline_shell`/`inline_shell_timeout`（`:1369-1370`） |
| `tools/skills_guard.py` | **811 行** | §10 的安全扫描器：`TRUSTED_REPOS`（`:24`）、`INSTALL_POLICY`（`:26`）、`scan_skill`（`:586`）、`should_allow_install`（`:660`）、`_resolve_trust_level`（`:780`） |
| `tools/skills_hub_official.py` | **388 行** | §6/§10 的 `optional-skills/` 作为源 `official`（`:39-40`） |
| `tools/skills_hub_github.py` | **524 行** | §10 的"直接 GitHub 标识"（`_split_repo_id` `:158`） |
| `tools/skills_hub_skillssh.py` | **361 行** | §10 的 `skills.sh` 标识（`BASE_URL = "https://skills.sh"`、`SOURCE_ID = "skills-sh"`） |
| `tools/skills_hub_sources.py` | **430 行** | §10 的 well-known 端点（`BASE_PATH = "/.well-known/skills"` `:25`） |
| `tools/skills_hub_models.py` | **327 行** | §10 的 `TRUST_RANK`（`:113`） |
| `agent/curator.py` | **1100 行** | 策展器（curator）；**本章原文未点名**，此处仅供术语对照（见附录 A 末行） |

---

## 附录 C · 上游文档与源码的出入

核对锚：上游 commit `f5d1926110`（v0.21.3）。原文点名的绝大多数东西都在源码里**逐条命中**，另有 **5 处出入**逐条给出。

**C-1 · 已逐条命中的（结论：一致）**

- §3.1 `platforms` 限平台、且"不兼容则从系统提示词 / `skills_list()` / 斜杠命令隐藏" → `agent/skill_utils.py:143 skill_matches_platform`（`:128 skill_matches_platform_list`，`:137-138` 逐字 *"whether sys.platform is "linux" (pre-3.13) or "android" (3.13+)"*）。
- §3.2 条件激活四字段的**语义** → `agent/prompt_builder.py:1210 def _skill_should_show`，`:1224-1227` 逐字四条分支（`fallback_for_toolsets` / `fallback_for_tools` / `requires_toolsets` / `requires_tools`），与文档表格一一对应。
- §3.3 `required_environment_variables` 在 `skill_view` 时自动登记透传 → `tools/skills_tool.py:438-442` 逐字 *"Only vars actually set pass through to sandboxed execution (execute_code, terminal)."* + `register_env_passthrough(available_env_names)`。
- §4 "旧的 `prerequisites.env_vars` 作为向后兼容别名仍受支持" → `tools/skills_tool_setup.py:48` 逐字 *"Merge required_environment_variables, setup.collect_secrets and legacy prerequisites.env_vars into one deduped, validated list (first entry wins)."*
- §4.1 `skills.config` 命名空间 + `hermes config migrate` 扫描"未配置的设置" → `agent/skill_utils.py:693 SKILL_CONFIG_PREFIX = "skills.config"`；`hermes_cli/config.py:994` docstring 逐字 *"Return skill-declared config vars (``skills.config.<key>``) that are missing or empty."*；`hermes_cli/config.py:1530`、`:1536`。
- §4.1 `hermes config show` 下的 "Skill Settings" → `hermes_cli/config.py:2978` 逐字 `_section("Skill Settings")`。
- §4.1 运行时注入块 `[Skill config (from ~/.hermes/config.yaml):` → `agent/skill_commands.py:194` 逐字 `parts.append(f"[Skill config (from {display_hermes_home()}/config.yaml):")`。
- §4.2 `required_credential_files` 缺失触发 `setup_needed` → `tools/skills_tool.py:453` 逐字 `setup_needed = setup_needed or bool(missing_cred_files)`。
- §4.2 "以只读绑定挂载挂进 Docker" → `tools/environments/docker.py:443` docstring 逐字 *"``-v host:container:ro`` args for credential files, skill dirs and cache dirs. Read-only so the"*；实现 `:462` 逐字 `args.extend(["-v", f"{entry['host_path']}:{entry['container_path']}:ro"])`。
- §4.2 "同步进 Modal（创建时 + 每条命令之前）" → `tools/credential_files.py:1-5` docstring 逐字 *"which credential files (skill ``required_credential_files`` + ``terminal.credential_files`` config), skill dirs, and host cache dirs to mount or sync in, **at creation and before each command**."*
- §5.4.1 激活消息 `[Skill directory: /abs/path]` → `agent/skill_commands.py:248` 逐字 `parts += ["", f"[Skill directory: {skill_dir}]", _SKILL_DIR_NOTE]`。
- §5.4.1 `${HERMES_SKILL_DIR}` / `${HERMES_SESSION_ID}` 两个模板 token → `agent/skill_preprocessing.py:15` 逐字 `_SKILL_TEMPLATE_RE = re.compile(r"\$\{(HERMES_SKILL_DIR|HERMES_SESSION_ID)\}")`；`:35` docstring 逐字 *"tokens without a value stay in place"*（"没有会话时原样保留"）。
- §5.4.1 `skills.template_vars: false` 全局关掉替换 → `hermes_cli/config_defaults.py:1366` 逐字 `"template_vars": True,`；`agent/skill_preprocessing.py:99 if cfg.get("template_vars", True):`。
- §5.4.2 内联 shell 写法、默认关闭、4000 字符上限、`[inline-shell error: ...]` 标记、以技能目录为 CWD → `agent/skill_preprocessing.py:17` 逐字 `_INLINE_SHELL_RE = re.compile(r"!\`([^\`\n]+)\`")`；`:19` 逐字 `_INLINE_SHELL_MAX_OUTPUT = 4000`；`:101 if cfg.get("inline_shell", False):`；`:72` 逐字 `return f"[inline-shell error: {exc}]"`；`:80` docstring 逐字 *"Replace every !`cmd` snippet with its stdout, run with the skill dir as CWD."*；`hermes_cli/config_defaults.py:1369-1370` 逐字 `"inline_shell": False,` / `"inline_shell_timeout": 10,  # seconds per !`cmd` snippet`。
- §5.4 网关剥掉 `[[as_document]]` 并把媒体按文件附件投递 → `gateway/platforms/base.py:1414` docstring 逐字 *"Remove MEDIA: tags and [[audio_as_voice]] / [[as_document]] markers so they never render"*、`:1423` 逐字剥离、`:4085 force_document = "[[as_document]]" in response`。
- §7 蓝图"不是新对象类型" → `tools/blueprints.py:3-4` 逐字 *"A blueprint is NOT a new object type: it is an ordinary skill whose frontmatter declares ``metadata.hermes.blueprint`` (``schedule`` required; optional ``deliver`` [default "origin"], …"*；`:33 deliver: str = "origin"`。
- §7 安装蓝图只登记成建议、**绝不**自动排程 → `hermes_cli/skills_hub.py:562 _announce_blueprint` docstring 逐字 *"Offer an installed skill's ``metadata.hermes.blueprint`` via /suggestions — never auto-scheduled (installing must not silently create a recurring job)."*
- §8 四个来源 → `cron/suggestions.py:39` 逐字 `VALID_SOURCES = frozenset({"catalog", "blueprint", "usage", "integration"})`。
- §8 驳回闩锁、待办上限 → `cron/suggestions.py:162` docstring 逐字 *"Dismiss a suggestion (latched — never re-offered for its dedup_key)."*；`:37 MAX_PENDING = 5`。
- §8 目录四项 → `cron/suggestion_catalog.py` 逐字 title：`:40 "Daily briefing"`、`:59 "Important-mail monitor"`、`:83 "Weekly review"`、`:100 "Workday start reminder"`。
- §8 "接受一条建议调用同一个 `cron.jobs.create_job`，没有第二套任务引擎" → `cron/suggestions.py:7` 逐字 *"Accepting calls ``cron.jobs.create_job`` with the stored ``job_spec`` — no second job engine;"*；`cron/jobs.py:1755 def create_job`；`cron/blueprint_catalog.py:4` 逐字 *"there is no second job engine"*。
- §8 `auxiliary.monitor` → `hermes_cli/config_defaults.py:741` 逐字 `"monitor": _aux(60),   # important-mail 0-10 scorer; high-volume, small model fine`。
- §9.1 `hermes skills publish skills/my-skill --to github --repo owner/repo` → `hermes_cli/subcommands/skills.py:148-153`（`--to` choices `["github", "clawhub"]`、`--repo` default `""`）。
- §9.2 `hermes skills tap add owner/repo` → `hermes_cli/subcommands/skills.py:168-169` 逐字 `tap_add.add_argument("repo", help="GitHub repo (e.g. owner/repo)")`。
- §6 `hermes skills browse`（标 "official"）→ `hermes_cli/subcommands/skills.py:38-43`（`--source` choices 含 `"official"`）；`tools/skills_hub_official.py:37-40` 逐字 *"Discoverable via the Skills Hub as source "official" with "builtin" trust."* / `SOURCE_ID = "official"` / `TRUST_LEVEL = "builtin"`。
- §10 三种第三方发现模型 → `tools/skills_hub_github.py`（直接 GitHub 标识）、`tools/skills_hub_skillssh.py:1` 逐字 *"Skills Hub skills.sh adapter: catalog discovery via skills.sh, content via GitHub."*、`tools/skills_hub_sources.py:22-25` 逐字 *"Read skills from a domain exposing /.well-known/skills/index.json."* / `BASE_PATH = "/.well-known/skills"`。
- §10 扫描器四类检查 → `tools/skills_guard.py:42` 逐字 `category: str       # "exfiltration" | "injection" | "destructive" | "persistence" | "network" | ...`；外泄一组 `:115` 逐字 `# ── Exfiltration: shell commands leaking secrets ──`。
- §10 `--force` 可覆盖非危险发现、`dangerous` 不覆盖 → `tools/skills_guard.py:661` docstring 逐字 *"``(allowed, reason)`` from verdict + trust; *force* overrides every block except a dangerous verdict on …"*；`:673` 逐字 `"--force does not override a dangerous verdict."`。

**C-2 · 出入（5 条，逐条给 `文件:行号` + 逐字引文）**

**① `official` 不是"信任级别"，而是"来源（provenance）"，其信任级别是 `builtin`。**

- 原文 §10 逐字把 `official` 与其余三档并列：*"Trust levels: `builtin` … `official` — from `optional-skills/` in the repo（built-in trust, no third-party warning）…"*。
- 源码里**信任级别**只有三档（+一档未文档化的 `agent-created`）：`tools/skills_guard.py:26-33` 的 `INSTALL_POLICY` 键为 `"builtin"` / `"trusted"` / `"community"` / `"agent-created"`；`tools/skills_hub_models.py:113` 逐字 `TRUST_RANK = {"builtin": 2, "trusted": 1, "community": 0}`。
- `official` 是**来源 id**：`tools/skills_hub_official.py:39 SOURCE_ID = "official"`、`:40 TRUST_LEVEL = "builtin"`；`tools/skills_guard.py:781` docstring 逐字 *'"official" is provenance, not a user-controlled GitHub id like "official/<repo>";'*，`:786-787` 逐字 `if src == "official":` / `return "builtin"`。
- **结论**：术语层面**对不上** —— 文档的"四档信任级别"实为"三档信任级别 + 一个来源 id"。

**② `trusted` 的仓库清单少了一个：`NVIDIA/skills`。**

- 原文 §10 逐字：*"`trusted` — from openai/skills, anthropics/skills, huggingface/skills"*。
- 源码 `tools/skills_guard.py:24` 逐字：`TRUSTED_REPOS = {"openai/skills", "anthropics/skills", "huggingface/skills", "NVIDIA/skills"}`；`:23` 逐字注释 *"NVIDIA-verified skills each ship a signed `skill.oms.sig` + governance `skill-card.md`."*
- **结论**：清单**少列一项**。

**③ 条件激活字段少列了一个：`session_platforms`。**

- 原文 §3.2 只列四字段（`requires_toolsets` / `requires_tools` / `fallback_for_toolsets` / `fallback_for_tools`）。
- 源码 `agent/skill_utils.py:635` 逐字：`_CONDITION_KEYS = ("fallback_for_toolsets", "requires_toolsets", "fallback_for_tools", "requires_tools", "session_platforms")`；同处 `:633-634` 逐字注释：*"``session_platforms`` is the gateway-channel gate: session platforms the skill is FOR (hidden from the index elsewhere), unlike ``platforms:`` (host OS)."*；判定分支在 `agent/prompt_builder.py:1216-1220`。
- **结论**：**漏列**（不是矛盾）—— 第五个条件字段 `session_platforms` 是网关信道闸门，与 `platforms:`（宿主 OS）不是一回事。

**④ §2 示例树里的 `productivity/ocr-and-documents/` 在该锚点的仓库树里不存在。**

- 原文第 38–40 行逐字：`│       ├── SKILL.md` / `│       ├── scripts/` / `│       └── references/`（挂在 `productivity/ocr-and-documents/` 下）。
- 实测 `git ls-tree -r f5d1926110 --name-only | grep '^skills/productivity/'`：**没有** `ocr-and-documents` 目录；同名内容只以文档站页面存在 —— `website/docs/user-guide/skills/bundled/productivity/productivity-ocr-and-documents.md`；仓库内与该主题相关的只有 `skills/productivity/pdf/references/ocr-extraction.md`。
- 同一棵树里的另两项（`research/arxiv/`、`productivity/`）**在场**（`skills/research/arxiv/SKILL.md` 282 行）。
- **结论**：示例树含一个**该锚点不存在的目录**（作为"结构示意"可读，作为事实不实）。

**⑤ 第四档信任级别 `agent-created` 未在文档中提及。**

- 源码 `tools/skills_guard.py:31-32` 逐字：
  ```
      # "ask" = error to the agent (retry without the flagged content); only when skills.guard_agent_created is on.
      "agent-created": ("allow",  "allow",   "ask"),
  ```
  以及 `:53` 逐字 `trust_level: str    # "builtin" | "trusted" | "community" | "agent-created"`、`:784-785` 逐字 `if src == "agent-created":` / `return "agent-created"`。
- **结论**：文档的"信任级别"清单**不完整**。

**C-3 · 两处说明（不算出入）**

- `check_fn`：本章原文**通篇没有**这个词。这里引用的是**平台适配器可用性探针**字段（`gateway/platform_registry.py:50` 逐字 `check_fn: Callable[[], bool]`），与 `SKILL.md` frontmatter 无关；工具注册表也使用同名探针，不能把这个名字专属于平台适配器。
- `hermes skills publish --to`：原文 §9.1 的标题是 "To the Skills Hub"，而 CLI 参数 `--to` 的取值是注册表名 `["github", "clawhub"]`（`hermes_cli/subcommands/skills.py:150-151`）。文档示例 `--to github` 落在取值范围内；标题用"技能中心"、参数用"注册表"，是命名口径差异，**不构成冲突**。
