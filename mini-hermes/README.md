# mini-hermes

一个从零手写的、真正能跑的 agent —— 目的是搞懂 Hermes 怎么实现的。

不要照着 Hermes 抄代码量，要抄**架构决策**。每一章读一篇官方 Developer Guide，
然后把对应的机制在 mini 版里实现一遍。

## 当前进度

| 章 | 官方文档 | mini 版状态 | 配套文档 |
|---|---|---|---|
| 1 | `architecture.md` | — （只是地图） | 《Hermes架构总览.md》 |
| 2 | `agent-loop.md` | ✅ **已实现并跑通** | 《中文精译-02-AgentLoop.md》《源码讲解-02-AgentLoop.md》 |
| 3 | `prompt-assembly.md` | ✅ **已实现并跑通** | 《中文精译-03-PromptAssembly.md》《源码讲解-03-PromptAssembly.md》 |
| 4 | `tools-runtime.md` | ✅ **已实现并跑通** | 《中文精译-04-ToolsRuntime.md》《源码讲解-04-ToolsRuntime.md》 |
| 5 | `context-compression-and-caching.md` | ✅ **已实现并跑通**（四个新模块 · **152 条测试** · 接线 · 真跑验证 · 差距清单 A+B 批 9 条已抄） | 《中文精译-05-ContextCompressionAndCaching.md》《源码讲解-05-ContextCompressionAndCaching.md》 |
| 6 | `session-storage.md` | ✅ **已实现并跑通**（**JSON → SQLite**：门面 + 5 个兄弟模块 · **107 条测试** · 逐条追加 · FTS5 检索 · 真跑验证 · 差距清单 14 条已抄） | 《中文精译-06-SessionStorage.md》《源码讲解-06-SessionStorage.md》 |
| 7 | `gateway-internals.md` | ✅ **已实现并跑通**（门面 + 3 个兄弟 + 适配器层 / 授权 / 钩子 / 进程身份 · **135 条测试**（该章交付时点；现为 214 条） · 反向验证 9/9 + 侧线 4/4 · 真跑走 `./mini gateway run` · 差距清单 14 条 · **+ 侧线：多 profile 入口层** `mini -p <名字>` / `mini profile create\|list\|show`） | 《中文精译-07-GatewayInternals.md》《源码讲解-07-GatewayInternals.md》 |
| 7.1 | `gateway-session-lifecycle.md` | ✅ **已实现并跑通**（5 个新模块：**状态机** `session_state.py` / **启动恢复** `run_startup.py` / **两层队列** `run_busy.py` / **agent 缓存** `agent_cache.py` / **后台巡检** `run_watchers.py` · `sessions` 表 +6 个状态列 · **214 条测试 / 17 组** · 反向验证 **14/14 干净变红** + 缓存副作用那条 **1 条干净变红** · 真跑（真 DeepSeek，临时 home）：缓存命中 1/1、覆盖通知、跨重启同一会话 · 差异表 26 条 / 差距清单 14 条 · 修掉第 7 章**两处语义错**） | 《中文精译-07b-GatewaySessionLifecycle.md》《源码讲解-07b-GatewaySessionLifecycle.md》 |
| 8 | `provider-runtime.md` | ✅ **已实现并跑通**（**providers/ 包**（723 行）+ **两段式解析** + `api_mode` 判定 + `source` + host 门控 + 别名归一（`756b493`）· **回退链** `agent/fallback.py` + **辅助路由** `agent/auxiliary.py` + 配置层 `hermes_cli/config.py`（`a391919`，回退链首次真正接线）· **219 条新契约测试**（106 + 113）· 全套件 **1,063 通过 / 0 失败**（双解释器）· 真跑：`source` 四来源 / host 门控三端点 / 别名归一 · 差异表 18 行 / 差距清单 A 批 10 + B 批 8 · **上游文档 3 处出入**（子代理继承链 / 触发点数 / key 门控）） | 《中文精译-08-ProviderRuntime.md》《源码讲解-08-ProviderRuntime.md》 |
| 10 | `micro-compaction.md` | ✅ **已实现并跑通**（`agent/micro_compaction.py` **531 行**（`MicroCompactionMixin`）+ 接线三处 · **108 条测试 / 9 组** · 全套件 **1,176 通过 / 0 失败**（双解释器）· 反向验证 **5/5 + 补丁 2/2 干净变红** · 真跑 **5/5 核验通过**（真 DeepSeek，脚本 `demos/ch10_real_run.py`）· 差异表 18 行 / 差距清单 A6 + B9 · ★ 补上「摘要被截断 ⇒ 丢弃半截」判据（`_summary_llm_meta()`，微压缩与批量两条路径）） | 《中文精译-10-MicroCompaction.md》《源码讲解-10-MicroCompaction.md》 |

## 章族表（★ 章号怎么数）

网关那条线做了三篇，但它们是**同一根主线的三次深入**（长什么样 → 两轮之间什么还在 →
一个进程服务多个 profile），所以按「**一章 + 扩展**」记，扩展**不占章号**：

| 主线章号 | 内容 | 文件 | 标签 |
|---|---|---|---|
| **第 7 章 · 网关（主线）** | `gateway-internals` | `中文精译-07-…` / `源码讲解-07-…` | `ch07-gateway` |
| └ 扩展 **7.1**（旧称第 8 章） | `gateway-session-lifecycle` | `中文精译-07b-…` / `源码讲解-07b-…` | `ch08-gateway-session-lifecycle` |
| └ 扩展 **7.2**（旧称第 9 章） | `multiplexing-gateway` | `中文精译-07c-…` / `源码讲解-07c-…` | `ch09-multiplexing-gateway` |
| **第 8 章** | `provider-runtime` | `中文精译-08-…` / `源码讲解-08-…` | `ch08-provider-runtime` |
| **第 9 章** | `programmatic-integration` | `中文精译-09-…` / `源码讲解-09-…` | —（mini 未实现：用户 2026-10-12 决定） |
| **第 10 章（当前）** | `micro-compaction` | `中文精译-10-…` / `源码讲解-10-…` | `ch10-micro-compaction` |
| **第 11 章（下一章）** | `state-db-recovery` | 待建 | — |

三条规则：

- **章号 = 主线计数**（扩展用 `7.1 / 7.2`，不占号）；
- **文件编号 = 全局序**：`07b` / `07c` 表示"它属于第 7 章的家族"；下一章仍叫 `-08-`；
- **标签名不重打**（`ch08-*` / `ch09-*` 是那两个扩展的历史标签，改它会打断旧引用）；
  正文里对旧称（"第 8 章 / 第 9 章"）的说法保留，读到时认成 `7.1 / 7.2` 即可。

## 章节顺序（★ 官方有**两份**口径，别混）

官方文档里有**两份**"推荐顺序"，用途不同（2026-10-10 核对上游）：

| 口径 | 出处 | 是什么 |
|---|---|---|
| **A · 速成 10 篇** | `developer-guide/architecture.md` 的 **Recommended Reading Order** | "如果你刚接触这个代码库"的**入门清单**（10 项，含 provider-runtime / adding-providers / acp-internals） |
| **B · 目录顺序** | `website/sidebars.ts` 的 Architecture 分组（14 项）+ Extending 分组 | **侧边栏罗列顺序**（就是目录树顺序，涵盖全部 49 篇） |

**我们实际走的**：第 2~7 章按"主线优先"自己排（方向与 A 一致，顺序不同：A 把 provider-runtime 排第 4，
我们放到了后面）；**第 8 章起按 B**（用户 2026-10-09 要求看官方推荐顺序，采用的位次口径）——注意章号按上面的**章族表**数：网关那三篇 = 第 7 章 + 扩展 7.1/7.2。

### A · Recommended Reading Order（官方"入门 10 篇"）的进度

```text
#  页面                              我们的进度
1  architecture                       ✅ 第 1 章（读文档，未实现）
2  agent-loop                         ✅ 第 2 章
3  prompt-assembly                    ✅ 第 3 章
4  provider-runtime                   ✅ 第 8 章（★ 唯一"两份口径都点名"的一篇，已还）
5  adding-providers                   ⬜
6  tools-runtime                      ✅ 第 4 章
7  session-storage                    ✅ 第 6 章
8  gateway-internals                  ✅ 第 7 章
9  context-compression-and-caching     ✅ 第 5 章
10 acp-internals（IDE 集成）           ⬜
```

### B · 侧边栏 Architecture 分组（14 项）的进度

```text
位次  页面                              我们的进度
  1   architecture                      ✅ 第 1 章（读文档）
  2   codebase-ownership                ⬜
  3   agent-loop                        ✅ 第 2 章
  4   prompt-assembly                   ✅ 第 3 章
  5   context-compression-and-caching   ✅ 第 5 章
  6   micro-compaction                  ✅ 第 10 章
  7   gateway-internals                 ✅ 第 7 章
  8   gateway-session-lifecycle         ✅ 7.1
  9   multiplexing-gateway              ⬜（第 7 章侧线做了它的一半：多 profile 入口层）
 10   completion-backlog-delivery       ⬜ 52 行（"投递债"）
 11   session-storage                   ✅ 第 6 章
 12   state-db-recovery                 ⬜ 107 行（与第 6 章配对）
 13   provider-runtime                  ✅ 第 8 章（= A 的第 4 项）
 14   programmatic-integration          ⬜ ← **下一章（第 9 章）**
```

**差额一句话**：A 是"少而精的必读路线"（10 篇），B 是"目录顺序"（14 + Extending 那一片）。
A 里我们还欠 **adding-providers / acp-internals** 两篇；
B 里我们还欠 codebase-ownership / micro-compaction / multiplexing-gateway /
completion-backlog-delivery / state-db-recovery / programmatic-integration 六篇。

（"插件 / 技能 / 工具群"那一族在 developer-guide 的 Extending 分组里，仓库里的占位仍写着"第 10 章"。）

## 环境 / 依赖

**零第三方依赖。** 只用 Python 标准库（`urllib` `json` `threading` `queue`
`concurrent.futures` `subprocess` `sqlite3`…）。这是设计选择不是疏忽——
手搓 `urllib` 发 HTTP，你才能看清 API payload 的真实长相。

- **Python 版本**：需要 **3.9+**（3.9.6 和 3.13.14 都已实测跑过全部测试）
- **不用建虚拟环境**：没依赖可隔离。`requirements.txt` 是空的（里面写了为什么）
- **不用 `pip install`**：`./mini` 就是入口
- **发布形态**：源码 + 启动脚本 + README。**venv 不是可分发的东西**
  （绑平台、绑 ABI、里面全是绝对路径），别人要的是能读懂、能跑起来的源码

启动脚本会自己挑解释器：`$MINI_PYTHON` → `.venv/bin/python3` → `PATH` 上的 `python3`。
（将来某章真引入了第三方库，就在 `requirements.txt` 里加上，启动脚本不用改。）

## 给别人跑起来

```bash
# 1. 拿到源码
git clone <仓库地址> && cd mini-hermes     # 或者直接解压

# 2. 放一个 API 密钥（DeepSeek）
mkdir -p ~/.mini-hermes
echo 'DEEPSEEK_API_KEY=sk-你的key' >> ~/.mini-hermes/.env

# 3. 跑
./mini -q "这个目录里有哪些文件？"
./mini            # 交互式

# 在你自己的项目目录里跑 —— AGENTS.md 会被自动加载
cd ~/我的项目 && /path/to/mini-hermes/mini

# 嫌路径太长？链到 PATH 上。启动器会自己解析符号链接，
# 所以"用户的项目目录在哪"照样认得（靠 MINI_LAUNCH_CWD）。
ln -s /path/to/mini-hermes/mini /usr/local/bin/mini
cd ~/我的项目 && mini
```

> ⚠️ **`mini` 不会自己被装到 PATH 上**——它是源码树里的一个脚本（本项目的形态就是
> "源码 + 启动脚本 + README"，不做 `pip install`）。所以从别的目录调用时：
> 要么写绝对路径，要么按上面那样链一个过去。
>
> 📌 直接敲 `mini` 而报 `command not found`，是**路径问题**，不是没装好。

密钥查找顺序（先找到的赢，真实环境变量永远优先于文件）：

| 顺序 | 位置 | 用途 |
|---|---|---|
| 1 | 环境变量 `MINI_ENV_FILE` 指的路径 | CI / 容器 |
| 2 | `$MINI_HOME/.env`（默认 `~/.mini-hermes/.env`） | **推荐给别人用这个** |
| 3 | `~/.hermes/.env` | 本机便利：复用已装好的 Hermes 密钥 |

没配密钥时的报错会直接告诉你上面这三条路径，不会只说一句"key 为空"。

## 怎么定制它的行为（第 3 章）

**不用改 Python。** 提示词的输入面都在 `$MINI_HOME`（默认 `~/.mini-hermes/`）：

| 文件 / 位置 | 改什么 | 可信吗 |
|---|---|---|
| `SOUL.md` | **身份**（人格、常驻行为）。没有就用硬编码的默认身份 | 你自己写的 → 命中注入模式只警告 |
| `MEMORY.md` | 跨会话事实 | 同上 |
| `USER.md` | 用户画像 | 同上 |
| `config.json` | `platform_hints`（平台提示的 append/replace）、`context_file_max_chars` | 坏配置一律当没配，不会让 agent 起不来 |
| 项目里的 `AGENTS.md` 等 | **仓库规则**（见下表优先级） | 跟仓库克隆进来的 → 命中就阻断 |

项目上下文文件的**优先级**（**跨类型**首个匹配者胜出，只加载一种）：

| 优先级 | 文件 | 搜索范围 |
|---|---|---|
| 1 | `.mini.md` / `MINI.md` | 从 cwd 一路向上到 git root |
| 2 | `AGENTS.md` | ★ **从 git root 到 cwd 链式合并**（每层目录各贡献一个文件，带来源标签） |
| 3 | `CLAUDE.md` | 只看 cwd |
| 4 | `.cursorrules` | 只看 cwd |

`AGENTS.md` 那一档为什么是链式的：仓库根的放"到处都适用的规则 + 路由表"，
各子目录的放"这一块的规则"——**链式合并让"走到哪加载到哪"自动发生**。
只加载 cwd 那份的话，根的全局规则会在你进入子目录时**静默丢失**。
（同一目录里 `AGENTS.override.md` > `AGENTS.md` > `agents.md`，首个非空者胜出。）

命令行的临时提示（**只作用于本次请求，不写进历史**）：

```bash
./mini --system "这次回答尽量短"          # 或者 MINI_EPHEMERAL_SYSTEM_PROMPT=...
```

**想验证定制生效了没有**：进 REPL 敲 `/prompt`，它把三层分开显示。

## 命令

```text
/exit    退出
/new     开新会话（清空历史 + 重置预算 + **重建系统提示词**）
/prompt  看三层系统提示词（★ 验证"哪块归属哪一层、顺序对不对"用这个）
/todo    看 agent 的任务清单（★ 跨进程验证"从历史重建"用这个）
/tools   看工具：**发给模型的** vs **注册了但没发的**（★ 第 4 章：一眼看出是哪道闸门关着）
/sessions  看最近 10 个会话（带预览）—— 库里有什么，一眼看到（★ 第 6 章）
/search 词 跨会话全文检索（FTS5；中文走 LIKE 兜底那条路）（★ 第 6 章）
/budget  看迭代预算用了多少
Ctrl-C   中断当前 turn
```

命令行参数：

```text
-q "问题"          单次提问（失败时退出码非 0）
-p/--profile 名    用哪个 profile（= 哪套 home/凭据/会话库）。**与真实版对齐**：真实版 `-p` 就是 --profile
                   不写 = 默认 profile（行为与以前一个字节不差）
--provider 名      provider（默认 deepseek）。★ 以前它是 `-p`，第 7 章侧线把它让给了 profile
-m/--model 名      模型名（默认取 provider 的默认模型）
--max-turns N      迭代预算（默认 500）
--resume           恢复最近一次会话
--quiet            只输出最终答复（配合 -q）
--debug            异常时直接把 traceback 打到 stderr（或设 MINI_DEBUG=1）
--system "..."     临时系统提示（只作用于本次请求，不写进历史）
--platform cli     cli 或 tui（决定平台提示）
--list-toolsets    列出所有 toolset（名字 / 说明 / 工具 / 在不在默认捆里）后退出 —— **不需要密钥**
--doctor           跑网络体检后退出 —— **不需要密钥**

子命令（第 7 章）：
gateway run|status|stop|hooks    跑/看/停网关（`gateway run` 前台阻塞；不需要密钥就能 status）
profile create|list|show         管 profile（`profile create lab --clone` 从默认 profile 起步）
```

**多 profile（第 7 章侧线）—— 一个 profile 就是一套 home**：

```bash
./mini profile create lab --clone   # 开一个实验 profile：复制默认的配置与身份（.env / SOUL.md）
                                    #   **不复制记忆**（MEMORY.md 是"经历过什么"，不是配置）
./mini -p lab                       # 之后一切都在 lab 自己的家里跑（会话库/日志/钩子/密钥都分开）
./mini profile list                 # 有哪些 profile
./mini profile show lab             # lab 家里有什么、几个会话
```

三件要知道的事：

- **`-p` 与真实版对齐**（真实版 `-p` 就是 `--profile`）；provider 现在只有长参数 `--provider`。
- **不存在的 profile 会报错，不会静默用默认 profile** —— 静默回落意味着"你以为在沙箱里乱来，
  实际在主 profile 上乱来"，而且不报错。
- **profile 之间不共享记忆与凭据**（物理分家：两个 `state.db`、两个 `.env`），
  所以"我记得你昨天说…"在另一个 profile 里会断 —— 这是设计，不是 bug。

**查 toolset（第 4 章那道"注册 ≠ 暴露"的闸门）**：

```bash
./mini --list-toolsets     # 不需要密钥、不联网
```

它回答的是"这套代码里有哪些 toolset、每个里面有哪些工具、哪些在**默认捆**里"。
REPL 里的 `/tools` 看的是**三件事分开**的版本 ——
① 已注册 ② 被 toolset 放行 ③ `check_fn` 此刻说可用不可用（例：`web_search` 有没有 key）。
两者共用同一份实现（`toolsets.describe_toolsets()`），所以不会漂。

**状态文件都在 `$MINI_HOME`（默认 `~/.mini-hermes/`）**：

```text
SOUL.md            身份（可选）
MEMORY.md          跨会话记忆（可选）
USER.md            用户画像（可选）
config.json        配置（可选）
state.db           会话库（SQLite，可 --resume；WAL 模式下还有 state.db-wal / -shm）
logs/errors.log    异常 traceback —— "报了错但没法查"的解药
.env               密钥（可选，见上文查找顺序）
```

## 出问题先跑体检

```bash
python3 doctor.py          # 约 10 秒，判定"这次卡住是谁的锅"
./mini --doctor            # 等价写法（同样不需要密钥）
```

它分层查：环境 → DNS（几个地址、顺序）→ 逐个连通性 → 分层计时（DNS/TCP/TLS/首字节）
→ 真实调用，最后给一句结论：**你的网络 / DeepSeek 侧 / 还是该上流式**。

**怎么亲手验证状态重建**：跑一个多步任务 → `/exit` → `./mini --resume` → `/todo`。
清单应该还在（`revision` 不是 0）。这跟"网关每条消息新建 agent 也不丢状态"是同一套机制。

## 发布前自检

```bash
python3 tests/test_packaging.py        # 18 条：零依赖 / 无硬编码路径 / 密钥发现链 / 符号链接调用 / 写死路径扫描 /
                                       #        命令行旗标契约（宣传的 `mini --旗标` 必须真的存在）
python3 tests/test_loop_mechanisms.py  # 57 条：agent loop 的机制（含「用量是每轮的」）
python3 tests/test_prompt_assembly.py  # 155 条：提示词装配（47 个函数 / 13 组，含字节稳定性）
python3 tests/test_tools_runtime.py    # 89 条：工具运行时（AST 发现 / toolset 闸门 / check_fn 抖动保护 / 结果契约）
python3 tests/test_compression.py      # 152 条：压缩与缓存（token 记账 / 边界 / 四阶段 / 契约 / 缓存标记 / 窗口表）
python3 tests/test_session_storage.py  # 107 条：会话存储（建库 / 加列 / 世代 / 血缘 / 检索 / 写入礼节 / 隔离）
python3 tests/test_gateway.py          # 214 条：网关（会话键 / 两级守卫 / 授权 / 命令表 / 交付 / 钩子 /
                                       #   进程身份 / profile 作用域 / 入口层 / ★ 第 8 章：会话生命周期 ·
                                       #   两层队列 · 缓存与巡检）
# 全套件：792 条断言 / 0 失败（3.9.6 与 3.13.14 双解释器一致）

# ⚠️ 改了代码但测试结果"看着没变"？先清字节码缓存再跑：
#      find . -name __pycache__ -type d -exec rm -rf {} +
#    Python 的 .pyc 失效判据是 (mtime, 文件大小)。做"两块对调"这类编辑时
#    文件长度**完全相同**，mtime 又落在同一秒 → 缓存被判为新鲜，
#    实际跑的是旧字节码。（本项目真踩过，排查了两轮。）
```

> **测试跑起来是按组报告的**（第 3 章那个文件），一眼看得出失败的是哪一类保证。
> 每组守什么承诺、具体断言是什么，见《源码讲解-03-PromptAssembly.md》§12。
> 它还配了一条**未注册自检**：定义了 `test_` 函数却没登记进 `GROUPS` 表 → 直接报错，
> 防止"写了但从来没跑"这种静默失败。

`test_packaging.py` 的用处：**一旦某个未来章节偷偷引入了第三方库，它会立刻红**——
而 README 和 `requirements.txt` 还在说"零依赖"，那种不一致比一开始就有依赖糟糕得多。

它靠 AST 扫真实 import（正则会把注释里的 `import openai` 也算上），
再查模块来源是否在标准库目录里。

**`test_prompt_assembly.py` 里最该看的一条**（第 3 章的立章之本）：

> 同一会话内反复构建系统提示词，**字节必须完全相同**。
> 不缓存的话，每轮都会重读 `SOUL.md` / `AGENTS.md` / `MEMORY.md`，
> 而且每轮结果可能不同（记忆被改了 / cwd 变了 / 跨天了）——
> **这个 bug 没有症状，只有账单**（prompt 缓存每轮失效，成本翻倍）。

## 为什么不做成 `pip install mini-hermes`

**因为那会破坏这个项目最大的价值：目录结构和 Hermes 一一对应。**

`pip install` 要求把 `run_agent.py` / `model_tools.py` / `tools/` 收进一个包目录里，
而 Hermes 本身就是**从仓库根目录跑**的（`run_agent.py` 就在顶层，用启动脚本调）。

Hermes 的发布形态也是这个：源码 + 安装脚本，**没有 wheel、没有 PyPI 包**。
教学项目跟它保持一致，学习者翻对照表时才有意义。

如果哪天真的想要一个 `pip install` 版或者单文件可执行版，
那是另一条路（PyInstaller，会把源码编译成二进制）——
但对一个"目的是让人读源码"的项目，那是反方向。

## 结构（和 Hermes 一一对应）

```text
run_agent.py              ←  run_agent.py（AIAgent 中枢）
model_tools.py            ←  model_tools.py（★ 第 4 章：AST 发现 + toolset 解析 + 分发）
toolsets.py               ←  toolsets.py（★ 第 4 章：哪些工具**暴露**给模型 —— "注册 ≠ 暴露"）
hermes_state.py           ←  hermes_state.py（★ 第 6 章：门面 —— 连接 / 事务 / 写入礼节）
hermes_state_common.py    ←  hermes_state_common.py（★ 第 6 章：SCHEMA_SQL 与常量，唯一事实来源）
hermes_state_schema.py    ←  hermes_state_schema.py（★ 第 6 章：建表 / 声明式加列 / 版本门控）
hermes_state_sessions.py  ←  hermes_state_sessions.py（★ 第 6 章：会话行 upsert / 标题 / 血缘）
hermes_state_messages.py  ←  hermes_state_messages.py（★ 第 6 章：消息行 / 世代 / 两种读法）
hermes_state_search.py    ←  hermes_state_search.py（★ 第 6 章：FTS5 + LIKE 兜底 + 查询清洗）
hermes_constants.py       ←  hermes_constants.py（路径解析）
cli.py                    ←  cli.py（CLI 壳：界面逻辑，不含 agent 行为逻辑）
tools/registry.py         ←  tools/registry.py（★ 第 4 章：中央注册表 + check_fn TTL 缓存）
tools/approval.py         ←  tools/approval_detection.py（★ 第 4 章：危险检测的唯一事实来源）
tools/*.py                ←  tools/*.py（一个工具一个文件，**import 时自注册**）
agent/iteration_budget.py ←  agent/iteration_budget.py
agent/prompt_builder.py   ←  agent/prompt_builder.py（★ 第 3 章：内容层）
agent/system_prompt.py    ←  agent/system_prompt.py（★ 第 3 章：排序层 —— 三层装配）
agent/context_engine.py   ←  agent/context_engine.py（★ 第 5 章：上下文引擎 ABC —— 可替换的压缩策略）
agent/usage_anchor.py     ←  agent/usage_anchor.py（★ 第 5 章：真实用量锚点 + 只估算增量）
agent/context_compressor.py ← agent/context_compressor.py（★ 第 5 章：四阶段压缩 + 无进展守卫）
agent/prompt_caching.py   ←  agent/prompt_caching.py（★ 第 5 章：cache_control 断点 + "为什么不动请求"）
gateway/run.py            ←  gateway/run.py（★ 第 7 章：门面 —— 装配 mixin / 生命周期 / 单实例）
gateway/run_inbound.py    ←  gateway/run_inbound.py（★ 第 7 章：第 2 级守卫 / 命令表 / 忙时策略）
gateway/run_busy.py       ←  gateway/run_busy.py（★ 第 8 章：**两层队列** 单槽覆盖 + 溢出 FIFO + `_promote_queued_event`）
gateway/run_turn.py       ←  gateway/run_turn.py（★ 第 7 章：绑作用域 → 建 agent/缓存取 agent → 出站）
gateway/run_startup.py    ←  gateway/run_startup.py（★ 第 8 章：启动恢复四步 · `.clean_shutdown` 读过即删）
gateway/run_watchers.py   ←  gateway/run_watchers.py（★ 第 8 章：后台巡检 tick —— 空闲/压力/剪枝，**只放资源不结束对话**）
gateway/run_shutdown.py   ←  gateway/run_shutdown.py（★ 第 7/8 章：进程收尾；★ 第 8 章更正：**不结束会话**，只对正在跑的标软恢复）
gateway/session_state.py  ←  gateway/session.py::get_or_create_session（★ 第 8 章：**路由状态机** —— 硬>软>复用，时间永不轮转）
gateway/agent_cache.py    ←  gateway/run_agent_cache.py + agent_cache_pressure.py（★ 第 8 章：LRU/TTL/保护带 + **三条不许淘汰**）
gateway/session_keys.py   ←  gateway/session.py（★ 第 7 章：会话键**唯一来源** + main~ 命名空间）
gateway/authz.py          ←  gateway/authz_mixin.py（★ 第 7 章：授权四层 · 默认拒绝）
gateway/delivery.py       ←  gateway/delivery.py（★ 第 7 章：直接回复 / 指定目标）
gateway/hooks.py          ←  gateway/hooks.py（★ 第 7 章：生命周期钩子 · 路径调用时解析）
gateway/status.py         ←  gateway/status.py（★ 第 7 章：PID + 启动时间指纹 + gateway_state.json + 退出码 78）
gateway/profile_scope.py  ←  gateway/run.py::_profile_runtime_scope 等（★ 第 7 章：profile 作用域绑定）
gateway/adapters/base.py  ←  gateway/platforms/base.py（★ 第 7 章：**第 1 级守卫**三件套 + 陈旧自愈）
gateway/adapters/console.py ← plugins/platforms/<name>/adapter.py（★ 第 7 章：一个真能跑的适配器）
agent/secret_scope.py     ←  agent/secret_scope.py（★ 第 7 章：密钥作用域 + 多路复用 fail-closed）
hermes_cli/commands.py    ←  hermes_cli/commands.py（★ 第 7 章：CommandDef / busy_policy / 表驱动分发）
hermes_cli/profiles.py    ←  hermes_cli/profiles.py（★ 第 7 章：profiles_to_serve · **HOME 锚定**）
hermes_cli/gateway_cmd.py ←  hermes_cli/gateway*.py（★ 第 7 章：`./mini gateway run|status|stop|hooks`）
hermes_cli/profile_cmd.py ←  hermes_cli/profiles.py 的 CLI 侧（★ 第 7 章侧线：`mini profile create|list|show`）
hermes_cli/main.py        ←  hermes_cli/main.py（命令行入口）
hermes_cli/runtime_provider.py ← hermes_cli/runtime_provider.py
doctor.py                 ←  （mini 特有）分层体检：环境→DNS→逐地址连通性→计时→真实调用

依赖链：tools/registry.py ← tools/*.py ← model_tools.py ← run_agent.py ← cli.py

tests/test_loop_mechanisms.py   # 54 条机制自测（不联网，假 API 响应驱动）
tests/test_prompt_assembly.py   # 155 条提示词装配自测（不联网）
tests/test_tools_runtime.py     # 89 条工具运行时自测（★ 第 4 章：AST 发现 / toolset 闸门 /
                                #   check_fn 抖动保护 / 结果契约 / 端到端）
tests/test_gateway.py            # 214 条网关自测（★ 第 7 章：会话键 / 两级守卫 / 授权 / 命令表 /
                                #   交付 / 钩子 / 进程身份（PID+启动时间指纹）/ profile 作用域 A→B→A
                                #   / 组 14 profile 入口层：create 播种 · id 字母表 · clone 不带记忆 ·
                                #   解析不静默回落 · 两 profile 物理分家 · 真 CLI 子进程）
                                #   ★ 单实例那组**必须跨进程**测（同进程里"第二个网关"的 pid 就是自己）
                                #   ★ profile 那组必须连 `Path.home` 一起钉（profile 根是 HOME 锚定的）
tests/test_session_storage.py   # 107 条会话存储自测（★ 第 6 章：建库 / 加列 / 世代 / 血缘 /
                                #            检索 / 写入礼节 / 隔离 —— 都是临时 MINI_HOME）
tests/test_compression.py       # 152 条压缩与缓存自测（★ 第 5 章：token 记账 / 边界 /
                                #   四阶段 / 无进展守卫 / 契约 / 缓存标记 / 模型窗口表）
tests/test_packaging.py         # 18 条发布前自检（零依赖 / 无硬编码路径 / 密钥发现链 / 符号链接调用 /
                                #   写死路径扫描 / **命令行旗标契约**：README 与注释宣传的旗标必须真的存在）
demos/                          # 能跑出来的故障复现，比散文有说服力
  why_interactive_is_serial.py  # 为什么 interactive 工具必须打断并发（三个真实故障）
  why_slow_connect_matters.py   # 连接超时 ≠ 读取超时（75s vs 2s，要等约 80 秒）
```

**第 4 章为什么新增 `toolsets.py`**：

```text
tools/registry.py   →  "这个工具**存在**吗"（import 时自注册，来者不拒）
toolsets.py         →  "这一轮该不该把它**告诉模型**"（按平台/会话收窄）
```

两层分开的关键后果：**注册成功≠模型看得见**。注册了但没在任何 toolset 里点名，
工具会**静默消失**——不报错、日志干净、`registry.get()` 拿得到。
`/tools` 就是为了让这个静默变成可见的。

同一个工具还有另一道闸门 `check_fn`（"**这个环境里**它现在能用吗"）。
两道闸门的分工判据：**"换个会话答案会变吗？"** 会 → toolset；不会 → `check_fn`。

**第 3 章为什么把提示词拆成两个文件**：

```text
prompt_builder.py   →  每一块"内容是什么"（读文件 / 安全扫描 / 截断）
system_prompt.py    →  这些块"按什么顺序、归属哪一层"
```

改排序规则只动后者，新增一种上下文来源只动前者。Hermes 也是这么分的。

## 故意没做的（等对应章节）

流式输出 · **子目录 AGENTS.md 的逐步发现** · **存盘提示词的校验** · 插件段 · 外部记忆 provider ·
**技能系统与技能索引** · Anthropic/Codex 的 API 模式 ·
网关多平台 · 子 agent
（~~SQLite 会话存储~~ → 第 6 章已做）

详细清单和"砍掉的代价"见《中文精译-02-AgentLoop.md》和《中文精译-03-PromptAssembly.md》的附录 B。

> 📌 **第 4 章故意跳过的，按"抄结构不抄数量"分类**：
>
> **数量型**（抄了没意义，mini 只要证明机制存在）：
> - Hermes 的 `DANGEROUS_PATTERNS` 有 **107 条**，mini 只留 10 条 —— 结构一样，覆盖面不装。
> - Hermes 的 terminal 有 **7 种后端**（docker/ssh/modal/…），mini 只留 local，
>   但把"后端表"的位置留出来了（`tools/terminal.py` 顶部的注释）。
> - Hermes 的 `INLINE_TOOL_EXECUTORS` 有 **14 个**，mini 只有 1 个。
>
> **机制型**（有真实需求再补，现在做就是投机）：
> - **异步桥接**（`_run_async`）：mini 的工具全是同步的。Hermes 需要它是因为
>   有些工具（MCP、桌面 UI）本质是异步的，而且它的判据是
>   **"在哪个线程 + 有没有 running loop"**，不是"CLI 还是网关"。
> - **发现结果的磁盘缓存**（按 `(mtime, size)`）：Hermes 有 ~100 个工具文件，
>   每次 AST 扫要 ~145ms。mini 只有 6 个 → 现在缓存是纯开销。
> - **`check_fn` 缓存按 profile 分键**（`hermes_home_key()`）：mini 是单 profile 的。
>   ⚠️ 这是**有意的简化**，代价写在 `tools/registry.py` 的模块 docstring 里——
>   如果以后支持多 profile，一个 profile 的探测结果会被另一个复用。
> - **`scoped slots` / 影子 / override 双闸门**：Hermes 允许插件注册同名工具覆盖内置的，
>   但要过两道闸门（作者声明 `override=True` + 使用者 `config.yaml` 里 `allow_tool_override: true`），
>   而且用 `handler.__globals__["__name__"]` 防止"包一层 lambda 冒充自己的实现"。
>   mini 没有插件系统 → 第 10 章再回来加。
> - **`_DYNAMIC_SCHEMA_REWRITERS`**：发 schema 前动态改工具描述（Hermes 用它把
>   "可用工具清单"塞进 `execute_code` 的描述里）。mini 没有 `execute_code` 工具。
> - **MCP 发现**：Hermes 把它**故意**从模块级移走了（阻塞 120s 会冻结网关心跳，
>   issue #16856），改由每个入口自己在启动时跑。mini 没有网关。
>
> **顺手修好的两个真 bug**（不是"没做"，是"做错了被这一章的原则抓出来"）：
> - 提示词里一直宣传 `search_files`（"不要用 grep/find"），但注册表里**没有这个工具**
>   —— 正是这一章讲的"**在描述里点名不可用的工具**"。现在它真的存在了。
> - 工具结果从 `"[error] xxx"` 纯文本改成 **JSON 字符串**（Hermes 的契约：
>   *All handlers return a JSON string*），因为结果要**从历史里被重新解析**。

> 📌 **第 5 章故意没做的，分三类**：
>
> **规模型**（抄了没意义）：
> - 引擎解析链是**三跳**（`plugins/context_engine/<名字>/` → 通用插件系统 → 内置），
>   还有深拷贝保护和"找不到就警告回退"。mini 没有插件系统 → 等第 10 章。
> - 窗口的**九级降级链**（config → 缓存 → 端点探测 → models.dev → 硬编码表 → 256K 兜底）。
>   mini 只留"查证过的表 + 环境变量覆盖"，**宁可压早不可压晚**（理由：mini 没有被拒后补救）。
> - 提示词缓存"**不插标记**"的判据有 **10 类**（传输方式、自定义 provider 能力声明、
>   LiteLLM 路由、非缓存路由主动剥离…），mini 只有 3 类。
>
> **归属别章**：
> - **网关侧的会话维护（session hygiene，85%）**：进 agent 之前的兜底压缩，服务于"两轮之间长起来"的会话 → 第 7 章。
> - **摘要失败冷却阶梯**（60→300→900s，持久化在 `state.db`）：mini 单进程、无网关、
>   无 `state.db` → 它的消费者还不存在。
> - **微压缩 / 原生 Responses 压缩 / Codex 三条支线**：都不是本章主线。
>
> **这两处"语义不同"已经在差距清单 A+B 批（`f59d4f8`）抄平了**（保留原始差异描述以备查）：
> - **尾部条数地板**：原来 mini 用 `protect_last_n=20` 直接取"更宽松的" → 20 条以内的历史**什么都压不动**；
>   现在与真实版一致：封顶到 ≤8（`_MAX_TAIL_MESSAGE_FLOOR`）、下限 3（`tail_message_floor`）。
> - **配对清理**：原来 mini 给"缺结果"的调用**注入占位结果**；现在**剥掉调用** + in-flight 保护
>   （真实版注释：占位会被下游 `repair_message_sequence` 当孤儿丢掉）。
>
> 另外，读源码时**核出上游文档 7 处不一致**（最重的是"默认 50% 触发"——
> 源码对 <512K 窗口的模型抬到 75%，还要过 64K 地板与 85% 上限），
> 清单在《源码讲解-05》附录 C。

> 📌 **第 7 章（网关）没做的 14 条** —— 完整版（每条带"什么时候必须补"）见
> 《源码讲解-07-GatewayInternals.md》§十四。这里只留三类形状：
>
> **规模/SDK 类**（零依赖框架做不了）：
> - 22 个平台适配器 + `plugin.yaml` + `register_deferred` 延迟加载 → mini 只有 1 个 console 适配器
>   （**但接口形状照抄**：connect/disconnect/send + handle_message），全部机制靠它真能端到端跑。
> - relay/connector 契约（出站 WebSocket 通用适配器）——需要 connector 端。
>
> **需要消费者才成立**（没有消费者就不该先建机制）：
> - 私聊配对 / `dm_policy` 四态（单用户单平台，配对的消费者不存在）；
> - 主频道投递 / 跨平台投递（需要 cron 这类后台产出 + 多平台）；
> - `delivery_obligations`（投递债记账）→ 代价写清楚：**网关崩在"确认消息已发、回答未到"之间，
>   那笔确认会无声消失**（进程内状态）；
> - 监督者映射（systemd `RestartPreventExitStatus=78` / s6 finish→125 / launchd …）→ mini 不装服务，
>   所以退出码 78 目前**没有消费者**（但语义已定好：**退出码也是接口**）。
>
> **等有对应基座再补**：
> - 网关侧会话维护（85% 兜底压缩）——mini 的压缩在 agent 内部（⬜ 仍未做）；
>   缓存 agent 那半 ★ **第 8 章已补**（`agent_cache.py`：LRU/TTL/保护带 + 三条不许淘汰）；
> - 跨进程压缩锁、终端作用域（`TERMINAL_*` 整套安装/复位，要有 docker/ssh 后端才谈得上）、
>   多 profile 的 YAML 桥（`apply_yaml_bridge`）、`command:*` 通配钩子。
>
> **第 7 章还掉的三笔债**：**多 profile 多库**（第 6 章差距清单 #1，⚠️ 当时写的是
> "支持多 profile 时必须补，否则静默串库"）→ ✅ 本章还了：
> `profile_runtime_scope`（home contextvar + 密钥作用域，**不改 `os.environ`**）
> + `_db()` 走 `get_mini_home()` + `profiles_to_serve(multiplex)` + **A→B→A 测试**。
> 仍缺**终端作用域**（§十四 #9）。
> 另外两笔：跨进程压缩锁（⬜ 未做）、网关的输入归属（⚠️ 部分：有守卫与授权，
> 但**没有**把"这条消息是谁写的"记进库 —— 将来做压缩轮转仲裁时必补）。

> 📌 **第 8 章的债（还掉 / 新挂 / 不做）**：
>
> **本章还掉的**：
> - 第 7 章差距 B5 **`_gateway_accepted` 回执** —— 当初不抄的理由是"mini 没有内部队列，
>   回执没有消费者"；本章做了两层队列 → **理由消失** → 已补（`adapters/base.py`）。
> - 第 7 章差距 #7 的**缓存 agent 那半**（"每条消息新建 agent"）→ 已补（`agent_cache.py`）。
> - 第 7 章的两处**语义错**：`shutdown()` 结束所有会话（→ 进程退出不是对话边界）、
>   `shutdown()` 给所有会话 `touch()`（→ 废掉了恢复的 120 秒窗口）。两处都有断言守着。
>
> **本章新挂的**（详见《源码讲解-07b》§十三）：
> - **主动续跑**（恢复第 ④ 步）—— 要先给 `AIAgent` 加"没有用户消息也能起轮"的入口（第 2 章），成本高；
> - **stall 看门狗**（`HERMES_SESSION_STALL_TIMEOUT`，默认 300s）—— 要先给 agent 加 activity summary；
> - **老记录剪枝**（`prune_old_entries`）—— 要在存储层做（第 6 章），当前只抄了开关语义 + 诚实记债。
>
> **本章不做的**（代价已写）：
> - 单飞去重 / `sessions.json` 镜像 / `was_auto_reset` 三字段 / PII 脱敏 /
>   内存压力"每条目估算" / 多平台字段；
> - **压缩后淘汰 agent 的联动** —— mini 的压缩在 agent 侧、agent 每轮现读历史，
>   那个 bug **构造不出来**（加了就是死代码）；**什么时候它突然必需**：谁把历史搬进
>   agent 内存 / 谁给网关补 hygiene 压缩段，谁就得回来补这一句（漏了不报错，
>   表现为"历史回退"）。

> 📌 **第 6 章故意没做的，分三类**（对应差距清单 B 批的 10 条）：
>
> **归属别章**（消费者还不存在）：
> - **多 profile**（一 profile 一个库 + `profile_name` 列 + 回填闸门 + IDOR 范围）→ 第 7 章。
>   代价写明：**支持多 profile 时必须补，否则静默串库**（不报错，只是用了别人的数据）。
> - **跨进程压缩锁**（`compression_locks` / `session_turn_leases`）：mini 单进程。
> - **网关的输入归属**（`observed` / `platform_message_id` / `effect_disposition` 与异常写入方仲裁）。
>
> **规模型**（抄了没意义）：
> - **CJK 与子串索引**（`messages_fts_trigram` / `messages_fts_cjk`）：实测 **trigram 救不了
>   2 字中文词**（要求查询串 ≥3 字符），cjk 需要**编译扩展** → mini 只留 **LIKE 兜底**那条路
>   （真实版同名机制），并用 `/search` 当它的消费者。
> - **展示层**（`display_*` 5 列 + 6 个触发器 + 世代去重）：消费者是桌面端。
> - **计费全套**（11 列 + `session_model_usage` 三表）：mini 只记 input/output token 数。
> - **CLI 族**（`sessions recover` / `export_all` / `prune_sessions` / 导入限额）。
>
> **有意选择（和真实版不一样）**：
> - **不做 `repair_alternation`**：真实版对脏历史宽容（重放时修一修），mini 的
>   `_validate_alternation` 直接抛错。判据：真实版面对线上用户的历史，mini 面对开发期 ——
>   历史上出现 `user;user` 应该**炸给开发者看**，而不是悄悄修。
> - **不做 `api_content` 字节保真**：需要"临时层注入也要按字节重放"的消费者，mini 没有。
>
> 📌 **第 6 章 mini 反过来多做的三件**（真实版没有）：
> ① **拒绝打开比代码更新的库**（`SchemaTooNewError`）—— 挡住"用旧代码写新库"这类静默破坏；
> ② `/sessions` 与 `/search` 两个 REPL 命令 —— 给会话列表与 FTS5 一个**真实消费者**；
> ③ **提示词漂移告警**（`--resume` 时"存盘 vs 现在"的字节比较）—— 第 3 章欠的账。

> 📌 **流式输出排在哪**：官方文档里**没有**聊天文本流式的专章
> （`streaming-tts.md` 讲的是流式语音合成，不是这个）。
> 它在代码里散在 `agent/chat_completion_stream_monitor.py`、`stream_delivery.py`、
> `stream_single_writer.py`、`stream_diag.py` 这几个文件，
> 文档侧只有 `agent-loop.md` 的回调表两行 + 网关的 streaming delivery 契约
> （在 `gateway/AGENTS.md` 里，不在公开文档）。
> **所以这块属于"必须看代码"的部分** —— 等讲到好时机再补进 mini 版。

> 📌 **第 3 章故意跳过的三个东西，都是"等有真实需求再补"**：
> - **子目录 AGENTS.md 逐步发现**（Hermes 的 `agent/subdirectory_hints.py`，236 行）：
>   mini 只有单体 AGENTS.md。真实需求是"大仓库里单文件会膨胀"。
> - **存盘提示词校验**（`_stored_prompt_matches_runtime`）：
>   `--resume` 时无法判断存的那份还对不对，现在只会继续用旧的。
> - **技能索引**：Hermes 的 volatile 层以它打头，stable 层还有个"先占位后回填"的技巧
>   （用哪版帮助指引取决于它）。mini 没有技能系统 → 第 10 章再回来加。
