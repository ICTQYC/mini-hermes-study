# 这个仓库是干什么的

逐章对照 Hermes 官方 Developer Guide，手写一个能跑的 mini-hermes（`mini-hermes/`）。
目标是**搞懂架构决策**（抄的是决策，不是代码量），不是抄代码量。

⚠️ **进度与数字不在这个文件里** —— 它们在 `mini-hermes/README.md`（章族表 + 进度表 + 套件数字）
和 git 标签（每章一个 `ch%02d-<slug>`，另有一个 `final-v1` 冻结点）里。
不在这里复制，是为了避免两处不一致（改一处忘另一处）。

## 新会话开工三步

**1. 读进度** —— `mini-hermes/README.md` 的章族表 / 进度表，再对一遍 `git tag`。
（维护者的技能文件里另存了一份同样的纪律与逐章续工说明，不在本仓。）

**2. 核实真实状态**（别信任何文档的自述，以 git 为准）：

```bash
git log --oneline -5          # 仓库根就是当前目录
git status --short            # 应该 0 未提交才算干净
git tag                       # 每章一个标签 ch%02d-<slug>，外加 final-v1
```

**3. 跑测试**（改动后必须跑，**看到 `0 失败` 再提交**）：

```bash
cd mini-hermes
find . -name __pycache__ -type d -exec rm -rf {} +    # ← 见下面「陈旧字节码」那条
for f in tests/test_*.py; do echo "── $f"; python3 "$f" | tail -1; done
```

用 **两个**解释器都跑：`/usr/local/bin/python3`（3.13，Homebrew）和 `/usr/bin/python3`（3.9）。
测试是纯标准库脚本（`python3 tests/test_xxx.py`），**不需要 pytest**。

## 不变量（踩过的都在这，别重踩）

**讲解质量**

- 讲解里**不许自造术语**；英文技术词的中译若不是中文技术圈通行说法，**第一次出现必须给定义**。
  能给代码或表格就别只给一个词。
- 三份文档职能不同，别互相串：**精译**（翻译为主、看懂原理、默认留薄）/
  **源码讲解**（差异、深挖、真问题与修法）/ **代码解读**（每章定稿后走读）。
- 不要往打印/输出里埋**没测量过**的断言 —— 打印该是观察到的结果。
- 行数一律 `wc -l`（`split("\n")` 会多算一行）；H1 只在第 1 行、全篇只一个 H1。
- 用户可见的字符串里**别挂会漂的编号**（章号、版本）—— 写死了就会过期。

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
- 建了临时目录（`mkdtemp`）就要在 `finally` 里 `rmtree` —— 仓里还有几个老文件漏了这步，
  跑完看看 `$TMPDIR` 别留垃圾（写法照 `demos/ch13b_real_run.py` 的 `finally: shutil.rmtree(...)`）。
- 改完 `**/*.py` 若遇到「测试结果和代码对不上」，先清 `__pycache__`：
  `.pyc` 的失效判据是 `(mtime, size)`，**两块长度相同的代码对调**会被判成新鲜缓存。

**版本管理**

- 每章结束 commit + 打标签 `ch%02d-<slug>`。
- **标签语义 = 那一章「末」的完整状态**（不是第一个提交）→ 当前章标签会随该章文档
  后续修改往后移；**开新章前必须冻结上一章标签**。
- 被文档引用的文件改了行数（或文档本身被改），**引用方与被引用方的数字要一起改**；
  行号引用要按**引文反查真身**重取，**不许按位移机械重映射**。
- 发布：本仓作为学习资料公开；上游文档镜像 `官方文档-Developer-Guide/` 是 gitignore 的，
  **不随仓发布**（读者自行同步），所以发布前不要把它加进跟踪集。

**凭据**

- 密钥只写在 `~/.hermes/.env`（和 `~/.mini-hermes/.env` 的非空覆盖位）。
- **不进 shell 历史、不在聊天里出现值**；代码里取凭据必须走 `get_secret`，不用 `os.getenv`。
- 公开仓里**不出现真实密钥、个人路径、个人身份信息**；示例一律通用化。

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
| `agent/skill_utils.py` · `tools/skills.py` | `tests/test_skills.py` |
| `agent/context_engine.py` · `agent/engine_registry.py` | `tests/test_context_engine.py` |
| `agent/observer.py` | `tests/test_observers.py` |
| `hermes_cli/web.py` · `hermes_cli/web_assets/`（本地演示页面） | `tests/test_web.py`（+ `test_packaging.py`） |
| **新增/删除模块** · 动依赖 · 改模块白名单 | `tests/test_packaging.py`（**永远跑**：它是"发布前自检"） |

三条从归属图带出来的约定，在 mini 里的对应物：

1. **改动留在自己的子系统内** —— mini 有两道真实闸门：`tools/` 下的工具靠 AST 自动发现（加工具只需改
   `tools/` + `toolsets.py`），而 `toolsets.py` 里的 `TOOLSETS` 是**白名单**：不登记进 toolset 的工具，
   模型永远看不到它。
2. **"跑源码目录对应的镜像测试"** —— mini 没有镜像层次，用上面那张表；拿不准就全套跑
   （双解释器跑法见上面「新会话开工三步」第 3 条）。
3. **两个子系统都涉及，更窄的那个拥有改动** —— 现成例子：`gateway/run_turn.py`，回合作用域绑在
   **这条消息所属的** profile 上（更窄处），而不是网关启动时的 `self.home`。

## 协作约定（直接影响怎么干活）

- 中文交流，**直接、不啰嗦**，别铺垫、别复述问题。
- 复杂流程**画图**（HTML/SVG 或 mermaid）比长篇文字有效；图随对应文档走。
- 用户会**主动叫停跑偏** —— 侧线做完就收口，别主动扩张。
- 改动后会要求「再检查一遍、确保所有部分都能做好」= **穷尽式复核**
  （数值声明、文档与实测是否一致、端到端副作用），而不只复查改动的那一处。
- 有人报故障时**先顺着他的操作路径查** —— 报故障的人常先怀疑自己，但那条路径往往就是对的。

## 目录

```
.                      仓库根（.git 就在这）
├── AGENTS.md          本文件（贡献与接续手册）
├── README.md          项目入口（怎么跑、内容地图、体例说明）
├── LICENSE            MIT
├── 收尾总览.md         全图谱 / 差距总账 / 冻结与发布清单（全库的账）
├── mini-hermes/       手写的实现（目录结构与 Hermes 一一对应）
│   ├── agent/         回合循环、提示词装配、上下文压缩、缓存标记、技能、引擎插槽、观察者
│   ├── tools/         工具（AST 自动发现，加工具只需改 tools/ + toolsets.py）
│   ├── hermes_cli/    provider 解析、配置
│   ├── skills/        内置技能（用户级技能在 <MINI_HOME>/skills/，优先级更高）
│   ├── demos/         故障复现与真跑脚本（离线可跑）
│   └── tests/         test_*.py，每个文件自带 GROUPS 登记表
├── 中文精译-0N-*.md    第 N 章的精译（01 是架构总览：地图章）
├── 源码讲解-0N-*.md    第 N 章的差异与深挖
└── 官方文档-Developer-Guide/   上游文档镜像（gitignore，不跟踪）
```
