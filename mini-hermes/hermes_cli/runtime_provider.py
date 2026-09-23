"""provider → (api_mode, base_url, api_key, model) 的解析。

对应 Hermes 的 hermes_cli/runtime_provider.py + hermes_cli/auth.py。

这一层存在的原因：模型和 provider 是**两件独立的事**。
同一个模型名可以走 OpenAI 官方、OpenRouter、本地 vLLM、国内 MaaS……
所以配置里存的是"provider 名字"，运行时才解析成真正要 POST 的 URL 和 key。

密钥只从环境变量 / .env 文件读，永不写进代码、永不打印
（Hermes 的铁律：secrets in .env, settings in config.yaml）。

═══════════════════════════════════════════════════════════════════════
给别人跑的时候要注意（这是本文件唯一"不通用"的地方）：

密钥文件的查找顺序（先找到的赢，前面的不会覆盖后面的环境变量）：
    1. 环境变量 MINI_ENV_FILE 指的路径
    2. ~/.mini-hermes/.env          ← 给别人用就写这里（$MINI_HOME/.env）
    3. ~/.hermes/.env               ← 方便本机：直接复用你已有的 Hermes 密钥

真实的 Hermes 用的是一条完整的凭证解析链（OAuth / 凭证池 / key 轮换 /
多 profile），这里只留了最朴素的版本。
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_mini_home

# provider 注册表：名字 → 默认端点和默认模型
PROVIDER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "default_model": "deepseek-chat",
        "api_mode": "chat_completions",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "api_mode": "chat_completions",
    },
    "anthropic": {
        # 注意：Hermes 里 anthropic 走的是 anthropic_messages 模式（另一套消息格式），
        # 我们这一章只实现 chat_completions，所以这里标注出来是为了"知道差异在哪"。
        "base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-5",
        "api_mode": "anthropic_messages",
    },
}


# ── 模型 → 上下文窗口（token）────────────────────────────────────────────
# ★ 这张表是**外部事实**，会过时，所以：一条一行、带来源、允许环境变量覆盖。
#
# 为什么必须知道这个数：压缩阈值 = 窗口 × 0.50。窗口填**大**了 → 永远不压缩 →
# 溢出报错；填**小**了 → 压得太勤 → 多花辅助调用的钱。**宁可填小**。
#
# 查证过的：
#   deepseek-chat  128_000  —— DeepSeek 官方 API 文档：deepseek-chat / deepseek-reasoner
#                              对应 DeepSeek-V3.2，128K 上下文。查证于 2026-09-23。
#                              ⚠️ 若该别名已指向 V4（1M），要重新查证再改这一行。
# 没查证的**不填**（不猜）：例如 gpt-4o-mini / claude-sonnet-4-5 目前落到下面的
# MODEL_CONTEXT_DEFAULT。要用它们就先查官方文档，再把数字连同来源加进来。
MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "deepseek-chat": 128_000,
}
# 表里没有的模型用这个（保守值：压早一点，不冒溢出的风险）
MODEL_CONTEXT_DEFAULT = 64_000


def resolve_context_length(model: str) -> int:
    """模型 → 窗口大小。环境变量 MINI_CONTEXT_LENGTH 优先（用来试不同窗口）。

    环境变量的值解析不出来时**报错**而不是静默用默认值 —— 静默的话你会以为
    自己设了 8_000、实际在用 64_000，压缩行为完全对不上。"""
    raw = os.environ.get("MINI_CONTEXT_LENGTH", "").strip()
    if raw:
        try:
            val = int(raw)
        except ValueError:
            raise RuntimeError(f"MINI_CONTEXT_LENGTH 必须是整数，收到 {raw!r}") from None
        if val <= 0:
            raise RuntimeError(f"MINI_CONTEXT_LENGTH 必须为正，收到 {val}")
        return val
    return MODEL_CONTEXT_WINDOWS.get(model, MODEL_CONTEXT_DEFAULT)


def env_file_candidates() -> List[Path]:
    """密钥文件的候选路径，按优先级排序。"""
    out: List[Path] = []
    override = os.environ.get("MINI_ENV_FILE")
    if override:
        out.append(Path(override).expanduser())
    out.append(get_mini_home() / ".env")           # 自己的（推荐给别人用）
    out.append(Path.home() / ".hermes" / ".env")   # 本机便利：复用 Hermes 的
    return out


def load_env_file(path: Optional[Path] = None) -> List[Path]:
    """把密钥文件里的键读进 os.environ（**不覆盖已存在的环境变量**）。

    真实环境变量永远优先于文件 —— 这样 CI / 容器里能直接覆盖，不用改文件。

    返回：实际读到并生效的文件列表（好让调用方/测试能断言读了哪个）。
    """
    candidates = [path] if path is not None else env_file_candidates()
    used: List[Path] = []

    for env_path in candidates:
        if not env_path.exists():
            continue
        used.append(env_path)
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            # ★ 只认**非空**值：先出现的文件里的空行**不算数**，继续往下找。
            #
            # 不这样做的后果（实测过）：在 `$MINI_HOME/.env` 里留一行
            # `DEEPSEEK_API_KEY=`（准备以后填），因为跳过判据是"**键名**是否已存在"，
            # 它会先把键设成空字符串，于是后面 `~/.hermes/.env` 里那个**真值**
            # 被当成"已存在"跳过 —— 报错只说"密钥为空"，绝口不提是哪个文件干的。
            #
            # 语义代价：**不能靠写空值来"主动屏蔽"某个键**（本来也做不到，
            # 屏蔽该用环境变量覆盖或注释掉那一行）。
            if key and val and key not in os.environ:
                os.environ[key] = val
    return used


def resolve_provider(provider: str, model: Optional[str] = None) -> Dict[str, Any]:
    """解析出真正要用的 API 参数。解析不出来就明确报错，不猜。"""
    load_env_file()

    spec = PROVIDER_REGISTRY.get(provider)
    if spec is None:
        raise RuntimeError(
            f"未知 provider：{provider}（已注册：{', '.join(PROVIDER_REGISTRY)}）"
        )

    api_key = os.environ.get(spec["api_key_env"], "")
    if not api_key:
        # 报错必须包含"下一步该做什么"——别人第一次跑卡住的地方就在这
        own_env = get_mini_home() / ".env"
        raise RuntimeError(
            f"provider {provider} 缺少密钥：环境变量 {spec['api_key_env']} 为空。\n"
            f"任选一种：\n"
            f"  1) 写进密钥文件（推荐，不会进 shell 历史、不会出现在进程列表里）：\n"
            f"       echo '{spec['api_key_env']}=你的key' >> {own_env}\n"
            f"  2) 设环境变量：export {spec['api_key_env']}=你的key\n"
            f"已查找的密钥文件：{', '.join(str(p) for p in env_file_candidates())}"
        )

    base_url = spec["base_url"]
    if spec.get("base_url_env"):
        base_url = os.environ.get(spec["base_url_env"], base_url) or base_url

    model_name = model or spec["default_model"]
    return {
        "provider": provider,
        "api_mode": spec["api_mode"],
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "model": model_name,
        # ★ 窗口大小随 spec 一起走 —— 压缩阈值 = 窗口 × threshold，
        #   没有它就只能吃默认值，等于"假装知道模型多大"。
        "context_length": resolve_context_length(model_name),
    }
