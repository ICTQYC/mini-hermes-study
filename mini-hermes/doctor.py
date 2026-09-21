#!/usr/bin/env python3
"""mini-hermes 体检 —— 一条命令判定"这次卡住是谁的锅"。

跑法：python3 doctor.py  （或 ./mini --doctor）

它按**分层**查，每层只回答一个问题：

  ① 环境     解释器 / 依赖 / 密钥文件   → 配置对不对？
  ② DNS      解析出几个地址、顺序如何   → 名字能不能变成 IP？
  ③ 连通性   每个地址单独连一遍          → 有没有"死地址"？
  ④ 分层计时 DNS/TCP/TLS/首字节         → 时间花在哪一段？
  ⑤ 真实调用 走我们自己的代码路径        → 端到端能不能通？

最后给一句**结论**。为什么值得固化：这三种情况的表象一模一样（"卡住不动"），
但处理方式完全不同 ——
  · 死地址      → 网络/DNS 问题，所有客户端都受影响，改 /etc/hosts 或等它好
  · API 报错    → DeepSeek 侧问题，看状态码
  · 都正常但慢  → 模型生成慢（那就是流式该上场了）
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BAR = "─" * 72
PROBE_TIMEOUT = 3.0        # 每个地址的探测超时（故意短，只用来判断"通不通"）
E2E_TIMEOUT = 20.0         # 端到端探测的上限。⚠️ 诊断工具自己绝不能挂住 ——
                           # 之前没设上限，最坏会等 2×180s（READ_TIMEOUT × 重试），
                           # 用户看到的就是"doctor 也卡死了"，比不诊断还糟。


def say(*args, **kw) -> None:
    """print + flush。

    ⚠️ 必须 flush：不然输出被重定向/管道时会被缓冲，
    一旦脚本卡住，你就只看到"什么都没有"——无法判断它卡在哪一段。
    （我自己就踩了这个：用 `| tail` 跑，结果彻底看不到进度。）
    """
    kw.setdefault("flush", True)
    print(*args, **kw)


def title(n: str, what: str) -> None:
    say(f"\n{BAR}\n{n}  {what}\n{BAR}")


def section_environment():
    title("① 环境", "解释器 / 依赖 / 密钥")
    import platform

    print(f"  Python      {platform.python_version()}  ({sys.executable})")
    print(f"  Python 版本 {'✓ 满足 3.9+' if sys.version_info >= (3, 9) else '✗ 太旧'}")

    third = []
    for mod in ("openai", "httpx", "requests"):
        try:
            __import__(mod)
            third.append(mod)
        except ImportError:
            pass
    print(f"  第三方包    {third if third else '（无 —— 符合设计）'}")

    try:
        from hermes_cli.runtime_provider import env_file_candidates, load_env_file, resolve_provider

        used = load_env_file()
        print(f"  读到的密钥文件 {[str(p) for p in used] or '（一个都没有）'}")
        print(f"  候选路径    {[str(p) for p in env_file_candidates()]}")
        spec = resolve_provider("deepseek")
        print(f"  provider    {spec['provider']}  模型 {spec['model']}")
        print(f"  端点        {spec['base_url']}  密钥 {'✓ 已解析（不显示）'}")
        return spec
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 配置解析失败：{exc}")
        return None


def section_dns(spec) -> list[str]:
    title("② DNS", "解析出哪些地址、顺序如何（顺序决定代价！）")
    host = spec["base_url"].split("//")[-1].split("/")[0]
    try:
        t0 = time.time()
        infos = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
        dt = time.time() - t0
    except OSError as exc:
        print(f"  ✗ 解析 {host} 失败：{exc}")
        return []
    addrs = []
    for i, info in enumerate(infos):
        ip = info[4][0]
        addrs.append(ip)
        print(f"  {i}: {ip}   {'← 客户端会先试这个' if i == 0 else ''}")
    print(f"  解析耗时    {dt*1000:.1f} ms")
    print(f"  ⚠️ 顺序在一段时间内固定、跨时间段会变；排头的那个决定每次调用的代价。")
    return addrs


def section_reachability(addrs: list[str]) -> tuple[int, int]:
    title("③ 连通性", f"每个地址单独连一次（超时 {PROBE_TIMEOUT}s）")
    good = bad = 0
    for ip in addrs:
        t0 = time.time()
        try:
            s = socket.create_connection((ip, 443), timeout=PROBE_TIMEOUT)
            s.close()
            good += 1
            print(f"  {ip:18s} {time.time()-t0:5.2f}s ✓ 通")
        except OSError as exc:
            bad += 1
            print(f"  {ip:18s} {time.time()-t0:5.2f}s ✗ 不通（{type(exc).__name__}）")
    if bad == len(addrs) and addrs:
        print(f"\n  🔴 {len(addrs)} 个地址**全部**不通 —— 这不是单个坏 IP，是非网络/服务级的故障。")
        print(f"     （实测过：DS 的 DNS 会轮换多个地址，全都不通时就是它那边或你出口的问题）")
    elif bad:
        print(f"\n  🟠 有 {bad}/{len(addrs)} 个地址连不通 —— 这是**你的网络/DNS**的问题，不是 DeepSeek 的。")
    else:
        print("\n  ✓ 所有地址都通。")
    return good, bad


def section_timing(spec):
    title("④ 分层计时", "DNS / TCP / TLS / 首字节 —— 时间花在哪一段")
    host = spec["base_url"].split("//")[-1].split("/")[0]
    path = "/" + spec["base_url"].split("//")[-1].split("/", 1)[1] + "/chat/completions" \
        if "/" in spec["base_url"].split("//")[-1] else "/chat/completions"

    body = json.dumps({"model": spec["model"], "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 1}).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {spec['api_key']}",
               "Content-Length": str(len(body))}

    t = {}
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
        t["DNS"] = time.time() - t0
        # 用第一个地址，能直接看出"排头地址是好是坏"
        ip = infos[0][4][0]
        t0 = time.time()
        sock = socket.create_connection((ip, 443), timeout=PROBE_TIMEOUT)
        t["TCP"] = time.time() - t0
        sock.settimeout(30)
        t0 = time.time()
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        t["TLS"] = time.time() - t0
        t0 = time.time()
        conn = http.client.HTTPSConnection(host, 443)
        conn.sock = tls
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        t["首字节+读完"] = time.time() - t0
        print(f"  连的是排头地址 {ip}")
        for k, v in t.items():
            print(f"  {k:12s} {v:6.3f}s")
        if resp.status >= 400:
            print(f"\n  🟠 HTTP {resp.status} —— **DeepSeek 侧**返回了错误：{raw[:200]!r}")
        else:
            print(f"\n  ✓ HTTP {resp.status}，总共 {sum(t.values()):.2f}s")
        tls.close()
        return t
    except OSError as exc:
        print(f"  ✗ 失败于某一段：{type(exc).__name__}: {exc}")
        print(f"  已完成：{ {k: round(v, 3) for k, v in t.items()} }")
        return t


def section_end_to_end(spec):
    title("⑤ 真实调用", "走 mini-hermes 自己的代码路径（_http_post）")
    import run_agent
    from run_agent import AIAgent, CONNECT_TIMEOUT, READ_TIMEOUT

    say(f"  代码里的常量：CONNECT_TIMEOUT={CONNECT_TIMEOUT}s  READ_TIMEOUT={READ_TIMEOUT}s")
    say(f"  但本探测把读取上限临时压到 {E2E_TIMEOUT}s —— 诊断工具自己绝不能挂住。")

    original_read = run_agent.READ_TIMEOUT
    run_agent.READ_TIMEOUT = E2E_TIMEOUT      # 只影响这次探测
    try:
        agent = AIAgent(spec)
        msgs = [{"role": "user", "content": "只回一个字：好"}]
        t0 = time.time()
        try:
            r = agent._http_post({"provider": spec["provider"], **spec}, msgs)
            dt = time.time() - t0
            text = (r.get("choices") or [{}])[0].get("message", {}).get("content", "")
            say(f"  ✓ {dt:.2f}s 拿到响应：{text[:40]!r}")
            return True, dt
        except Exception as exc:  # noqa: BLE001
            dt = time.time() - t0
            say(f"  ✗ {dt:.2f}s 失败：{type(exc).__name__}: {exc}")
            if dt >= E2E_TIMEOUT * 0.9:
                say(f"     ⚠️ 用满了探测上限 —— 说明**连接上之后对方不回数据**（不是 DNS/连接问题）。")
            return False, dt
    finally:
        run_agent.READ_TIMEOUT = original_read


def verdict(spec, addrs, good, bad, timings, ok, dt) -> None:
    print(f"\n{BAR}\n结论\n{BAR}")
    if spec is None:
        print("  ✗ 配置有问题 —— 先解决 ① 里报的错。")
        return
    if addrs and bad == len(addrs):
        print(f"  🔴 **服务或出口故障**：{len(addrs)} 个地址全部连不通。")
        print(f"     这不是 mini-hermes 的问题，也不是单个坏 IP —— 先看 DeepSeek 状态页 / 换网络试试。")
        print(f"     ⚠️ 这种情况下**不要**把 IP 钉进 /etc/hosts：地址会轮换，钉死反而更糟。")
    elif bad and good:
        tcp = timings.get("TCP", 0)
        print(f"  🟠 **你的网络/DNS 的问题**：{len(addrs)} 个地址里只有 {bad} 个不通。")
        print(f"     排头地址决定代价 —— 这次排头的是 {'坏的，付了 ' + f'{tcp:.2f}s' if tcp > 1 else '好的，几乎不花时间'}。")
        print(f"     所有解析这个域名的程序都受影响（Hermes / curl / 浏览器 / git…），只是各自的 connect 超时不同：")
        print(f"       默认客户端 ~75s   ·   Hermes ~10s   ·   mini-hermes {__import__('run_agent').CONNECT_TIMEOUT}s")
        print(f"     若长期只有某一个地址坏 → 可以考虑 /etc/hosts 钉到好的那个；")
        print(f"     但地址会轮换，钉之前先连续观察几天，别只看一次。")
    elif not ok:
        print(f"  🟠 **DeepSeek 侧的问题**：地址都通，但 API 调用失败 —— 看 ④ 里的 HTTP 状态码。")
    else:
        print(f"  ✓ 网络和 API 都正常（端到端 {dt:.2f}s）。")
        if dt > 2:
            print(f"     若仍觉得慢：model 生成本身要 {dt:.1f}s，而**没有流式**意味着这段时间你只能看转圈。")
            print(f"     → 这时候该上流式，而不是查网络。")
    print(BAR)


if __name__ == "__main__":
    # 行缓冲：确保重定向/管道时也能实时看到每一段（诊断工具的可观测性优先）
    try:
        sys.stdout.reconfigure(line_buffering=True)   # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    say("mini-hermes 体检 —— 目标是判定'这次卡住是谁的锅'（最多约 40 秒）")
    spec = section_environment()
    if spec is not None:
        addrs = section_dns(spec)
        good, bad = section_reachability(addrs) if addrs else (0, 0)
        timings = section_timing(spec)
        ok, dt = section_end_to_end(spec)
        verdict(spec, addrs, good, bad, timings, ok, dt)
