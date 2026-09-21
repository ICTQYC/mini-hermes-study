"""为什么代码里要把"连接超时"和"读取超时"分开 —— 一个 75 秒的悬案。

跑法：python3 demos/why_slow_connect_matters.py

背景（2026-09-15，所内网络实测）：
  用户反馈 `./mini` "非常慢"。查下来 DNS 只花 4ms、TLS 只花 30ms，
  但**有一次 TCP 握手花了 60 秒**。

  原因：api.deepseek.com 的 DNS 返回**两个** IP，其中一个从本机完全连不通：

      60.28.220.199     TCP 0.011s  ✓      （4/4 成功）
      123.125.246.121   TCP 超时    ✗      （4/4 失败）

  系统默认的连接超时是 ~75 秒，所以"DNS 挑到坏 IP" = 整轮对话白等 75 秒。

这个脚本不依赖外网，用 monkeypatch 强制 getaddrinfo 返回 [坏IP, 好IP] 的顺序，
然后对比两种写法 —— 结果自己会说话。
"""
from __future__ import annotations

import socket
import sys
import time

BAR = "─" * 74

# 实测出来的两个地址（写死在这里，脚本不需要 DNS 也不需要发请求）
GOOD_IP = "60.28.220.199"
BAD_IP = "123.125.246.121"      # 从所内网络连不通（SYN 无响应）

_real_getaddrinfo = socket.getaddrinfo


def force_bad_first(host: str, port: int, *args, **kwargs):
    """让 api.deepseek.com 先解析出坏 IP —— 复现"DNS 挑错了"那一刻。

    ⚠️ 签名必须带 port：`socket.create_connection` 调的是
    `getaddrinfo(host, port, family, type)`，参数是**位置**传的。
    少接一个参数，后面的 family/type 就会错位
    （我第一次就写漏了，结果 family 变成 443 → "ai_family not supported"）。
    """
    if host == "api.deepseek.com":
        return (_real_getaddrinfo(BAD_IP, port, *args, **kwargs)
                + _real_getaddrinfo(GOOD_IP, port, *args, **kwargs))
    return _real_getaddrinfo(host, port, *args, **kwargs)


def try_connect(timeout):
    """按给定的单地址超时去连（create_connection 会依次尝试每个地址）。"""
    t0 = time.time()
    try:
        sock = socket.create_connection(("api.deepseek.com", 443), timeout=timeout)
    except OSError as exc:
        return time.time() - t0, None, exc
    peer = sock.getpeername()[0]
    sock.close()
    return time.time() - t0, peer, None


def main() -> None:
    socket.getaddrinfo = force_bad_first

    print(BAR)
    print("DNS 返回顺序被强制成：[坏 IP, 好 IP]   ← 这就是卡顿发生的那一刻")
    print(BAR)

    print("\n① 用系统默认连接超时（不给 timeout 参数）")
    print("   等于把每个地址的等待上限交给内核 —— 在 macOS 上约 75 秒。")
    print("   ⏳ 真的会等这么久，等一下…")
    dt, peer, err = try_connect(None)
    if err:
        print(f"   → {dt:.2f}s 全部失败：{err}")
    else:
        print(f"   → {dt:6.2f}s 才连上（连到 {peer}）")
        print(f"     ⚠️ 前 {dt - 0.03:.1f}s 全花在**等那个坏地址超时**上。")

    print("\n② 用我们的写法（直接读代码里的常量，不用手抄，免得漂移）")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    from run_agent import CONNECT_TIMEOUT      # noqa: E402

    print(f"   每个地址最多等 {CONNECT_TIMEOUT}s（run_agent.CONNECT_TIMEOUT）")
    dt, peer, err = try_connect(CONNECT_TIMEOUT)
    if err:
        print(f"   → {dt:.2f}s 全部失败：{err}")
    else:
        print(f"   → {dt:6.2f}s 就拿到了好地址 {peer}")
        print(f"     ✓ {CONNECT_TIMEOUT}s 后放弃坏地址，自动落到下一个 —— 这就是 mini 现在的行为。")

    print(f"\n{BAR}")
    print("机制的要点：`socket.create_connection` 本来就会**依次尝试**")
    print("getaddrinfo 返回的每个地址。问题从来不是「没试第二个」，")
    print("而是「试第一个等太久了」。")
    print(BAR)

    print("""
所以 mini-hermes 里的 `_TwoTimeoutHTTPSConnection` 做了一件事：

    sock = socket.create_connection(addr, timeout=CONNECT_TIMEOUT)   # 2s
    sock.settimeout(READ_TIMEOUT)                                     # 180s

**为什么不能直接给 urlopen 一个短 timeout？**
因为 urlopen 的 timeout 是**一个值同时管连接和读取**。
给 5 秒 → 模型生成超过 5 秒的正常回答会被掐断；
给 180 秒 → 坏地址就白等 180 秒。
这两件事的合理上限差两个数量级，**必须分开**。

    ┌── 连接阶段 ──┐┌──────── 读取阶段（模型在生成）────────┐
    0    5s        5s                                    185s
    坏地址只能占这么久 ↑                             ↑ 这里可以很久
    
📌 这类问题的通用名字：**"连接超时"和"读取超时"是两个东西**。
   几乎所有生产级 HTTP 客户端（requests / httpx / okhttp / Go net/http）
   都把 connect timeout 和 read/response timeout 设成两个独立参数 ——
   这不是 API 冗余，是血换来的。""")
    print(BAR)


if __name__ == "__main__":
    main()
