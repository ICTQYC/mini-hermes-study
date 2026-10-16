# 按机制选择演示 / Chapter demonstrations

演示围绕“输入了什么 → 实际执行了什么 → 输出怎样影响下一步”。当前对话能直接体现的机制跟随对话展示；需要控制变量的机制使用可操作的隔离实验。不是每个章节都需要运行按钮。

启动 `./mini-hermes/mini --web`，打开本地页面，选择章节。无需安装前端工具或 Python 包。API Key 仍在“模型设置”中填写，仅保留服务内存。

## 哪些章节怎样演示

| 章节 | 形式 | 操作与观察重点 |
|---|---|---|
| 02 Agent 循环 | 当前对话 | 选择每次请求，看实际 messages、模型返回的 tool_calls、对应工具结果，以及下一次带着结果的请求。输入“不存在的文件”观察真实失败如何回填 |
| 03 提示词装配 | 当前对话 | 三层提示词原文、临时指导、逐次请求和稳定前缀；详见 [操作说明](WEB_PROMPT_ASSEMBLY.md) |
| 04 工具运行时 | 互动实验 | 在 file/default 间切换，比较已注册工具和模型可见 schema；写入再读取实验文件；选择 terminal 查看白名单闸门，本页不会执行终端命令 |
| 05 上下文压缩 | 互动实验 | 生成长对话，改变窗口/保护头尾参数，按实际阈值压缩；查看每条原消息的去向、新上下文、摘要的实际输入和固定输出 |
| 06 会话存储 | 当前对话 | 查看这段对话的真实 SQLite 行、活跃/归档标记和恢复形状；丢弃 Agent 内存后从当前库恢复，再续聊；搜索本会话记录并查看检索路径 |
| 08 Provider 运行时 | 互动实验 | 选择 400/429/503，运行真实 Agent 的回退路径；查看主模型/备用模型实际尝试、每次请求 ID 和错误/成功事件 |
| 10 微压缩 | 互动实验 | 连续点击“吸收下一个交换”，比较每一步的输入、滚动摘要、受保护头尾和输出。消息条数不变时也可能真实吸收，按 action 展示 |
| 11 状态库恢复 | 互动实验 | 先只读体检，注入 FTS 降级标记，再显式备份修复；查看体检报告、实际备份、保留下来的消息和搜索结果 |
| 13 技能系统 | 当前对话 | 首次请求里的技能索引 → skill_view 调用 → 真实正文返回 → 正文进入下一次请求。离线场景生成一个 demo-review 技能 |
| 14 上下文引擎 | 互动实验 | 同一份输入在内置、生成的示例插件、未知引擎间切换；查看真实发现/选择结果、策略输出及回落警告 |
| 15 只读观察者 | 当前对话 | 按回合查看实际事件和 turn_id / api_request_id / tool_call_id；追加一个会抛异常的观察者，再发送任务，观察失败放开是否仍让回合完成 |

当前共有 **5 个对话查看器 + 6 个互动实验**。

## 保留导览的章节

- 01 是架构地图，无需运行。
- 07 / 07b / 07c 的网关入口、队列、生命周期和 profile 隔离已有实现，但 Web 对话没有经过那套网关入口，因此不把它冒充成网关演示。保留文档阅读入口。
- 09 的程序化宿主集成未独立实现，12 主要是代码归属和投递系统边界，保留文档导览。

这些章节没有运行入口，后端也拒绝对应的演示回合。

## 当前对话查看器

离线时模型决策由固定脚本提供，Agent 循环、工具、会话存储和观察者实际执行；切换真实模型后，同一个查看器展示实际调用，不另造成功路线。

- 模型请求在传输前保存脱敏后的 messages，响应回来后补上实际 response；用 tool_call_id 将结果和下一次请求对应起来。若中断或错误导致没有下一次请求，页面会明确说尚未观察到回填。
- 存储页读取当前会话所属的数据库，显示原始行与恢复后可交给 Agent 的字段形状。恢复按钮只丢弃这个会话的 Agent 内存，不删除数据库，也不把它称为整个服务重启。
- 观察者页遵循既有载荷脱敏和大小限制；最多保留最近 1000 个事件，若移除旧事件会显示数量。模型原始请求仍可分别回看，沿用每会话 64 次、单次 messages 1 MB 的明确上限。
- 技能正文通过真实 skill_view 工具获取。普通模型可能没有选择读技能，此时界面只显示实际决策，不会补画一个读取成功步骤。

## 互动实验的边界

实验参数、文件、数据库和示例插件都在本次服务创建的临时目录中，按 Web 会话隔离，退出清理；不修改使用者的项目、配置、插件或状态库。操作期间不调用外部模型。

- 压缩/微压缩调用真实压缩器，但摘要来自明确标注的固定离线夹具。可以验证消息选择、配对、保护边界与摘要注入，不能据此评价真实模型的摘要质量。token 是本地估算。
- Provider 实验通过固定传输函数注入 HTTP 故障，真正运行 Agent 的回退状态机；没有向真实服务发送故障请求。
- 状态恢复只设置实验库的 FTS 降级标记，不物理破坏文件，不代表验证了所有物理损坏情形。
- 引擎实验只加载生成目录中的固定示例插件。它刻意丢弃中间消息来展示策略可替换，不是推荐的生产策略。
- 工具实验仅允许生成目录内的读写/列目录。terminal 可以用于观察可见性，但不会被本页分发执行。

若想让真实模型讲解实验，先切换真实模型模式，在该会话执行一次实验，再发送问题。模型会收到本次参数、指标及实测片段；长内容明确截取，完整记录仍在实验面板。切换运行模式会新建会话。保存密钥不触发请求，发送问题才调用 API 并可能计费。

## English

The UI now has five conversation inspectors (agent loop, prompt assembly, storage, skills, observers) and six interactive labs (tools, compaction, provider fallback, micro-compaction, database recovery, engine selection). Inspect actual requests, decisions, results, and their effects instead of a prefilled success checklist.

Conversation inspectors work with scripted offline decisions or a real model. Labs always run locally on isolated fixtures; summaries and injected provider errors are explicitly scripted, while dispatch, compression, routing, recovery, and engine selection use the real implementation. In live mode, send a question after a lab action to explain its measured excerpts. Guide-only chapters have no run action. No third-party packages are required.

## 验证入口

```bash
python3 mini-hermes/tests/test_web_chapters.py
python3 mini-hermes/tests/test_web_lessons.py
python3 mini-hermes/tests/test_web.py
```

测试只使用假 provider 与临时目录，不调用外部模型。源模块位于已有 hermes_cli 根下，发布自检自动扫描。

![当前对话里的循环过程](docs/assets/web-agent-loop.png)

![可操作的上下文压缩实验](docs/assets/web-compression-lab.png)
