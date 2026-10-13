"""ContextEngine —— 上下文管理的抽象基类（第 5 章 §1）。

## 为什么要有个 ABC

上下文管理是**有损**的：压缩会丢掉东西。而"什么该留"**强依赖场景** ——
代码仓库要留文件路径和命令，长对话要留用户的原话，法律文档要留条款编号。
Hermes 没法替所有人决定，所以它把这个决策做成一个可替换的接口：

    context:
      engine: "compressor"   # 默认：内置的有损摘要
      engine: "lcm"          # 示例：插件提供的"无损上下文管理"

## 三个约束（原文档明确写了）

1. **实现只负责策略**，不负责"什么时候被调用"——那是 agent loop 的事。
2. **插件引擎永远不自动激活**：用户必须显式写 `context.engine`。
   默认值 `"compressor"` 永远指向内置实现。
3. 引擎**可以**顺带暴露工具（比如无损引擎的 `lcm_grep`），
   但那是可选的第四项能力。

## mini 版与 Hermes 的差异（一句话）

Hermes 有插件解析链（`plugins/context_engine/<name>/` → 通用插件系统 → 内置），
mini 只保留**一条**：`<MINI_HOME>/engines/<name>/` 目录发现 +
`agent/engine_registry.py` 的选择 —— 因为 mini 没有通用插件系统，
也没有第二个发现根（见第 14 章清单 B 批）。
**接口形状保持一致**，这样以后加插件只是在解析处加分支。

## name（第 14 章）

选择/注册按 **`name`** 寻址（`context.engine` 里写的就是它），
所以 `name` 是 `@abstractmethod`：每个引擎必须自报家门
（`ContextCompressor.name == "compressor"`）。这一条决定
`tests/test_context_engine.py` 的 I3/I7 两条不变量。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class ContextEngine(ABC):
    """上下文引擎。子类必须实现 `name`、`should_compress` 和 `compress`。"""

    # ── 必需：名字（注册 / 选择的寻址键）────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """引擎短标识（如 ``"compressor"`` / ``"lcm"``）—— 注册与选择按它寻址。

        ★ 第 14 章补：与上游逐字一致（真实侧 `agent/context_engine.py:50-53`：
          ``Short identifier (e.g. 'compressor', 'lcm').``）。

        为什么必须是**必需项**、而且做成 ``@property`` + ``@abstractmethod``
        （而不是给一个"派生自类名"的默认实现）：

        - 名字是**选择键** —— `context.engine` 里写的就是它，`select_engine(name)`
          也按它查表。靠类名派生 ⇒ 改一次类名就**静默改掉**用户的选择键；
          ABC 强制每个引擎**自报家门**，这条不可能悄悄漂移。
        - 用 **属性**而不是普通类属性：实例可以按需计算（普通属性做不到），
          同时又保留"忘了实现 ⇒ 实例化即 `TypeError`"这条免费契约
          （见 `tests/test_context_engine.py` 的不变量 I3）。
        """

    # ── 必需的两项能力 ──────────────────────────────────────────────────────

    @abstractmethod
    def should_compress(self, messages: List[Dict[str, Any]]) -> bool:
        """现在该压缩了吗？

        ★ 判据必须**便宜**：这个方法每轮都会被问一次，而它拿不到"未来的 token 数"。
        真实用量要靠 `update_usage()` 喂进来（见下）。
        """

    @abstractmethod
    def compress(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """执行压缩，返回**新的**消息列表（不要原地改传进来的那份）。

        调用方负责把返回值写回它自己的历史 —— 引擎不碰 agent 的状态。
        """

    # ── 可选：从 API 响应里记账 ──────────────────────────────────────────────

    def update_usage(self, usage: Dict[str, Any], messages: List[Dict[str, Any]]) -> None:
        """把 provider 报出的真实用量记下来。

        `usage` 是 API 响应里的 `usage` 字段（`prompt_tokens` / `completion_tokens`）。
        默认实现什么都不做 —— 但**只要引擎想用真实数字，就必须实现它**，
        因为"本地估算不准"正是这一章存在的理由（§2.2.1）。
        """

    # ── 可选：暴露工具给 agent 调用 ──────────────────────────────────────────

    def tools(self) -> List[Dict[str, Any]]:
        """引擎想额外暴露的工具 schema（默认没有）。"""
        return []

    # ── 可选：给调用方看的诊断信息 ──────────────────────────────────────────

    def describe(self) -> str:
        """一行人类可读的状态（给 `/compress` 和 `/tools` 这类命令用）。"""
        return self.__class__.__name__

    # ── 可选：模型切换时重新解析阈值 ─────────────────────────────────────────

    def update_model(self, model: str, provider: str = "") -> None:
        """会话切到别的模型了。

        ★ 覆盖了本方法的引擎**拥有自己的压缩策略**，
        可以忽略 `compression.model_thresholds` 那张映射表（原文明确）。
        """
