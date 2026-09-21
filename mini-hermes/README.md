# mini-hermes

一个从零手写的、真正能跑的 agent —— 目的是搞懂 Hermes 怎么实现的。

不要照着 Hermes 抄代码量，要抄**架构决策**。每一章读一篇官方 Developer Guide，
然后把对应的机制在 mini 版里实现一遍。

## 当前进度

| 章 | 官方文档 | mini 版状态 | 中文精译 |
|---|---|---|---|
| 1 | `architecture.md` | — （只是地图） | 《Hermes架构总览.md》 |
| 2 | `agent-loop.md` | ✅ **已实现并跑通** | 《中文精译-02-AgentLoop.md》 |
| 3 | `prompt-assembly.md` | ⬜ 待做 | — |
| 4 | `tools-runtime.md` | ⬜ 待做（当前只有 4 个工具 + 注册表） | — |
| 5 | `context-compression-and-caching.md` | ⬜ 待做（`_preflight_compression` 是空函数） | — |
| 6 | `session-storage.md` | ⬜ 待做（现在是 JSON，要换 SQLite） | — |
| 7 | `gateway-internals.md` | ⬜ 待做 | — |

## 环境 / 依赖

**零第三方依赖。** 只用 Python 标准库（18 个模块：`urllib` `json` `threading` `queue`
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
```

密钥查找顺序（先找到的赢，真实环境变量永远优先于文件）：

| 顺序 | 位置 | 用途 |
|---|---|---|
| 1 | 环境变量 `MINI_ENV_FILE` 指的路径 | CI / 容器 |
| 2 | `$MINI_HOME/.env`（默认 `~/.mini-hermes/.env`） | **推荐给别人用这个** |
| 3 | `~/.hermes/.env` | 本机便利：复用已装好的 Hermes 密钥 |

没配密钥时的报错会直接告诉你上面这三条路径，不会只说一句"key 为空"。

## 命令

```text
/exit    退出
/new     开新会话（清空历史 + 重置预算）
/todo    看 agent 的任务清单（★ 跨进程验证"从历史重建"用这个）
/tools   看已注册的工具
/budget  看迭代预算用了多少
Ctrl-C   中断当前 turn
```

命令行参数：

```text
-q "问题"        单次提问（失败时退出码非 0）
--quiet          只输出最终答复（配合 -q）
--debug          异常时直接把 traceback 打到 stderr（或设 MINI_DEBUG=1）
--max-turns N    迭代预算
--resume         恢复最近一次会话
```

**状态文件都在 `$MINI_HOME`（默认 `~/.mini-hermes/`）**：

```text
sessions/*.json    会话历史（可 --resume）
logs/errors.log    异常 traceback —— "报了错但没法查"的解药
.env               密钥（可选，见上文查找顺序）
```

## 出问题先跑体检

```bash
python3 doctor.py          # 约 10 秒，判定"这次卡住是谁的锅"
```

它分层查：环境 → DNS（几个地址、顺序）→ 逐个连通性 → 分层计时（DNS/TCP/TLS/首字节）
→ 真实调用，最后给一句结论：**你的网络 / DeepSeek 侧 / 还是该上流式**。

**怎么亲手验证状态重建**：跑一个多步任务 → `/exit` → `./mini --resume` → `/todo`。
清单应该还在（`revision` 不是 0）。这跟"网关每条消息新建 agent 也不丢状态"是同一套机制。

## 发布前自检

```bash
python3 tests/test_packaging.py      # 7 条：零依赖 / 无硬编码路径 / 密钥发现链
python3 tests/test_loop_mechanisms.py # 54 条：agent loop 的机制
```

`test_packaging.py` 的用处：**一旦某个未来章节偷偷引入了第三方库，它会立刻红**——
而 README 和 `requirements.txt` 还在说"零依赖"，那种不一致比一开始就有依赖糟糕得多。

它靠 AST 扫真实 import（正则会把注释里的 `import openai` 也算上），
再查模块来源是否在标准库目录里。

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
run_agent.py            ←  run_agent.py（AIAgent，第 2 章的正文）
model_tools.py          ←  model_tools.py（工具发现 + 分发）
hermes_state.py         ←  hermes_state.py（会话存储，暂时是 JSON）
hermes_constants.py     ←  hermes_constants.py（路径解析）
cli.py                  ←  cli.py（CLI 壳：界面逻辑，不含 agent 行为逻辑）
tools/registry.py       ←  tools/registry.py（中央注册表）
tools/*.py              ←  tools/*.py（一个工具一个文件，import 时自注册）
agent/iteration_budget.py ← agent/iteration_budget.py
agent/prompt_builder.py ←  agent/prompt_builder.py（极简版）
hermes_cli/main.py      ←  hermes_cli/main.py（命令行入口）
hermes_cli/runtime_provider.py ← hermes_cli/runtime_provider.py

依赖链：tools/registry.py ← tools/*.py ← model_tools.py ← run_agent.py ← cli.py

tests/test_loop_mechanisms.py   # 54 条机制自测（不联网，假 API 响应驱动）
tests/test_packaging.py         # 7 条发布前自检（零依赖 / 无硬编码路径 / 密钥发现链）
demos/                          # 能跑出来的故障复现，比散文有说服力
  why_interactive_is_serial.py  # 为什么 interactive 工具必须打断并发（三个真实故障）
  why_slow_connect_matters.py   # 连接超时 ≠ 读取超时（75s vs 2s，要等约 80 秒）
```

## 故意没做的（等对应章节）

流式输出 · prompt 缓存标记 · 上下文压缩 · 记忆/技能注入 · 插件钩子 ·
Anthropic/Codex 的 API 模式 · SQLite 会话存储 · 网关多平台 · 子 agent

详细清单和"砍掉的代价"见《中文精译-02-AgentLoop.md》附录 B。

> 📌 **流式输出排在哪**：官方文档里**没有**聊天文本流式的专章
> （`streaming-tts.md` 讲的是流式语音合成，不是这个）。
> 它在代码里散在 `agent/chat_completion_stream_monitor.py`、`stream_delivery.py`、
> `stream_single_writer.py`、`stream_diag.py` 这几个文件，
> 文档侧只有 `agent-loop.md` 的回调表两行 + 网关的 streaming delivery 契约
> （在 `gateway/AGENTS.md` 里，不在公开文档）。
> **所以这块属于"必须看代码"的部分** —— 等讲到好时机再补进 mini 版。
