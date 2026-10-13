# 这个仓库是干什么的

逐章对照 Hermes 官方 Developer Guide，手写一个能跑的 mini-hermes（`mini-hermes/`）。
目标是**搞懂架构决策**（抄的是决策，不是代码量），不是抄代码量。

⚠️ **当前进度不在这个文件里** —— 它只写在技能 `hermes-architecture-walkthrough` 里。
不在这里复制，是为了避免两处不一致（改一处忘另一处）。

## 新会话开工三步

**1. 读技能** —— 唯一权威的进度与纪律来源：

```
skill_view(name='hermes-architecture-walkthrough')
```

里面有章节进度表、三份文档的职能边界、**当前章的「续工前必读」那一节**（做到哪了、
下一步是什么）、以及踩过的坑。

**2. 核实真实状态**（别信任何文档的自述，以 git 为准）：

```bash
git log --oneline -5          # 仓库根就是当前目录
git status --short            # 应该 0 未提交才算干净
git tag                       # 每章一个标签 ch%02d-<slug>
```

**3. 跑测试**（改动后必须跑，**看到 `0 失败` 再提交**）：

```bash
cd mini-hermes
find . -name __pycache__ -type d -exec rm -rf {} +    # ← 见下面「陈旧字节码」那条
for f in tests/test_*.py; do echo "── $f"; python3 "$f" | tail -1; done
```

用 **两个**解释器都跑：`/usr/local/bin/python3`（3.13，Homebrew）和 `/usr/bin/python3`（3.9）。

## 不变量（踩过的都在这，别重踩）

**讲解质量**

- 讲解里**不许自造术语**；英文技术词的中译若不是中文技术圈通行说法，**第一次出现必须给定义**。
  能给代码或表格就别只给一个词。
- 三份文档职能不同，别互相串：**精译**（翻译为主、看懂原理、默认留薄）/
  **源码讲解**（差异、深挖、真问题与修法）/ **代码解读**（每章定稿后走读）。
- 不要往打印/输出里埋**没测量过**的断言 —— 打印该是观察到的结果。

**协作**

- **动 dotfiles（`~/.zshrc`、`~/.gitconfig`…）、系统目录、凭据文件之前，先问用户**。
- **发现某处内容变少时，先问是不是用户删的**，别先假定是自己的改动损坏了
  （这个坑踩过，还 `git checkout` 恢复了、覆盖了用户的工作）。
- **不布置自学作业**。用户问就讲；他要深挖时才问「要写进文件吗」。

**代码与测试**

- 零第三方依赖是**设计选择**，不做 `pip install`。
- 测试必须能**反向验证**：把产物改回旧写法，测试要变红（而且要**干净地**变红，
  不是整个套件崩掉 —— 可诊断才算红）。
- 新增模块要同步 `mini-hermes/tests/test_packaging.py` 的 `PROJECT_MODULES`
  （它装的是**模块根**，`agent` 已在里面，所以 `agent/` 下的新文件自动被覆盖）。
- 测试**不许依赖运行环境的配置**（比如某个 key 恰好配了）。
- 改完 `**/*.py` 若遇到「测试结果和代码对不上」，先清 `__pycache__`：
  `.pyc` 的失效判据是 `(mtime, size)`，**两块长度相同的代码对调**会被判成新鲜缓存。

**版本管理**

- 每章结束 commit + 打标签 `ch%02d-<slug>`。
- **标签语义 = 那一章「末」的完整状态**（不是第一个提交）→ 当前章标签会随该章文档
  后续修改往后移；**开新章前必须冻结上一章标签**。
- **远程仓库 / 打包发布等全部章节做完再说** —— 在此之前不要建远程。

**凭据**

- 密钥只写在 `~/.hermes/.env`（和 `~/.mini-hermes/.env` 的非空覆盖位）。
- **不进 shell 历史、不在聊天里出现值**；代码里取凭据必须走 `get_secret`，不用 `os.getenv`。

## 改动落在哪、要跑哪些测试（第 12 章落地的「归属图」）

真实版有一篇 `codebase-ownership`（= 《中文精译-12》第一部分）：每个子系统对应哪些源码目录、文档入口，
以及"**测试镜像源码目录**"这条约定。mini 的 `tests/` 是**平铺**的（没有 `tests/tools/` 这种镜像层次），
所以那条约定在这里只能落成**一张显式映射表**（下表由各测试文件实际 import 的模块反推，不是凭印象列的）。
改代码前先看它，改完按行跑：

| 你改了 | 必须跑 |
|---|---|
| `agent/context_compressor.py` · `agent/usage_anchor.py` | `tests/test_compression.py` |
| `agent/micro_compaction.py` | `tests/test_micro_compaction.py`（+ `test_compression.py`：两条压缩路径共用摘要注入点） |
| `providers/*` · `hermes_cli/runtime_provider.py` | `tests/test_provider_runtime.py` |
| `agent/fallback.py` · `agent/auxiliary.py` · `hermes_cli/config.py` | `tests/test_fallback_and_aux.py` |
| `run_agent.py` · `model_tools.py`（主循环 / 工具编排） | `tests/test_loop_mechanisms.py` |
| 提示词装配（`agent/` 的 prompt builder / system prompt） | `tests/test_prompt_assembly.py` |
| `tools/` · `tools/registry.py` · `toolsets.py` | `tests/test_tools_runtime.py` |
| `gateway/*` · `hermes_cli/profiles.py` · `hermes_cli/main.py` | `tests/test_gateway.py` |
| `hermes_state*.py`（会话存储） | `tests/test_session_storage.py` |
| `hermes_state_errors.py` · `hermes_state_fts.py` · `hermes_state_repair.py` · `hermes_cli/sessions_cmd.py` | `tests/test_state_db_recovery.py`（+ `test_session_storage.py`） |
| **新增/删除模块** · 动依赖 · 改模块白名单 | `tests/test_packaging.py`（**永远跑**：它是"发布前自检"） |

三条从归属图带出来的约定，在 mini 里的对应物：

1. **改动留在自己的子系统内** —— mini 有两道真实闸门：`tools/` 下的工具靠 AST 自动发现（加工具只需改
   `tools/` + `toolsets.py`），而 `toolsets.py:53-70` 的 `TOOLSETS` 是**白名单**：不登记进 toolset 的工具，
   模型永远看不到它。
2. **"跑源码目录对应的镜像测试"** —— mini 没有镜像层次，用上面那张表；拿不准就全套跑
   （双解释器跑法见上面「新会话开工三步」第 3 条）。
3. **两个子系统都涉及，更窄的那个拥有改动** —— 现成例子：`gateway/run_turn.py:75-84`，回合作用域绑在
   **这条消息所属的** profile 上（更窄处），而不是网关启动时的 `self.home`。

## 用户偏好（直接影响怎么干活）

- 中文交流，**直接、不啰嗦**，别铺垫、别复述他的问题。
- **视觉型学习者**：复杂流程要画 HTML/SVG 图（存 `~/Desktop/study/LLM面试准备`，Typora 可看）。
- 他会**主动叫停跑偏** —— 侧线做完就收口，别主动扩张。
- 他改完会要求「再检查一遍、确保所有部分都能做好」= **穷尽式复核**
  （数值声明、文档与实测是否一致、端到端副作用），而不只复查改动的那一处。
- 遇到故障他习惯先怀疑自己 —— 而他的操作路径往往是对的，顺着它查常能撞出真 bug。

## 目录

```
.                      仓库根（.git 就在这）
├── AGENTS.md          本文件
├── mini-hermes/       手写的实现（目录结构与 Hermes 一一对应）
│   ├── agent/         回合循环、提示词装配、上下文压缩、缓存标记
│   ├── tools/         工具（AST 自动发现，加工具只需改 tools/ + toolsets.py）
│   ├── hermes_cli/    provider 解析、配置
│   └── tests/         test_*.py，每个文件自带 GROUPS 登记表
├── 中文精译-0N-*.md    第 N 章的精译
├── 源码讲解-0N-*.md    第 N 章的差异与深挖
└── 官方文档-Developer-Guide/   上游文档镜像（gitignore，不跟踪）
```
