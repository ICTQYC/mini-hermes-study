"""为什么 interactive 工具必须打断并发 —— 三个能跑出来的故障。

跑法：python3 demos/why_interactive_is_serial.py

背景：模型一轮里返回了 4 个工具调用，其中一个是 interactive 的（要等用户回答）。
      「并发执行」听起来更快，实际上会坏三件事，这里一样一样跑给你看。
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

BAR = "─" * 72


def title(n: str, what: str) -> None:
    print(f"\n{BAR}\n{n}  {what}\n{BAR}")


# ══════════════════════════════════════════════════════════════════
# 故障 1：两个线程同时往同一个终端写字 → 提示被擦掉
# ══════════════════════════════════════════════════════════════════
def demo_output_interleave() -> None:
    title("故障 1", "主线程在刷进度，子线程在弹窗 → 两者抢同一个终端")

    # 把 sys.stdin 换成一个「延迟 1.5s 才有输入」的管道 —— 模拟「用户还在看题」
    # （直接换 sys.stdin 而不是 dup2 fd 0：builtins.input() 读的就是 sys.stdin）
    r, w = os.pipe()
    saved_stdin = sys.stdin
    stdin_pipe = open(r, "r")          # 这个文件对象接管 fd r，由它负责关闭
    sys.stdin = stdin_pipe
    result: dict = {}

    def spinner() -> None:
        """主线程：模拟 CLI 的 thinking 指示器（每 0.08s 用 \\r 重绘同一行）。"""
        for i in range(28):
            sys.stdout.write(f"\r· 正在思考{'.' * (i % 4)}   ")
            sys.stdout.flush()
            time.sleep(0.08)

    def clarify_tool() -> None:
        """子线程：模拟 interactive 工具，弹窗等用户输入。"""
        sys.stdout.write("\n[审批] 允许执行 rm -rf /tmp/x ? (y/N) ")
        sys.stdout.flush()
        result["answer"] = input()          # ← 阻塞在这里，等 1.5s 后的那行输入
        sys.stdout.write(f"\n[审批] 收到回答：{result['answer']!r}\n")
        sys.stdout.flush()

    threading.Timer(1.5, lambda: os.write(w, b"y\n")).start()
    t = threading.Thread(target=clarify_tool)
    t.start()
    spinner()
    t.join(3)

    sys.stdin = saved_stdin
    stdin_pipe.close()          # 关掉 fd r
    os.close(w)

    print("\n\n↑ 看出来了吗：那行 `[审批] 允许执行...` 被 `\\r` 刷掉了（上面挤成一团）。")
    print("  主线程每 0.08s 用 `\\r` **重绘同一行**，它不知道子线程刚在上面写了一行问题。")
    print("  用户看到的是：一个闪动的 progress 指示器，问题在哪、问了什么，全看不见。")
    print("  （这就是 mini-hermes/cli.py 里 _on_thinking 用的 `end='\\r'` 手法。）")


# ══════════════════════════════════════════════════════════════════
# 故障 2：Ctrl-C 只能叫醒主线程，叫不醒卡在 input() 的子线程
# ══════════════════════════════════════════════════════════════════
def demo_sigint_cannot_reach_worker() -> None:
    title("故障 2", "Ctrl-C 到不了子线程 → 打断失效，进程退不出去")

    r, w = os.pipe()                      # w 全程不关 → 子线程会一直阻塞在 read 上
    saved_stdin = sys.stdin
    stdin_pipe = open(r, "r")
    sys.stdin = stdin_pipe

    started = threading.Event()

    def clarify_tool() -> None:
        sys.stdout.write("[审批] 允许执行？(y/N) ")
        sys.stdout.flush()
        started.set()
        try:
            input()                        # ← 永远等下去（管道里没人写，w 也不关）
        except EOFError:
            # 只有收尾时关掉管道写端才会到这里 —— 这本身就是个证据：
            # **这个线程连「退出」都只能靠外部拆管子**，自己等不到任何东西。
            sys.stdout.write("(子线程这时才被放出来)\n")
            sys.stdout.flush()
        sys.stdout.write("(这行在信号到达时永远不会被打印)\n")

    t = threading.Thread(target=clarify_tool, daemon=True)
    t.start()
    started.wait(2)
    time.sleep(0.2)

    print("→ 现在模拟用户按 Ctrl-C（给进程发 SIGINT）…")
    sys.stdout.flush()
    try:
        os.kill(os.getpid(), signal.SIGINT)  # 等价于终端里按 Ctrl-C
    except KeyboardInterrupt:
        print("  主线程收到 KeyboardInterrupt，它打算中止这一轮、退出程序。")

    time.sleep(0.3)
    print(f"  但那个子线程还活着吗？  is_alive() = {t.is_alive()}")
    print("  → True。它卡在 input() 上，信号永远送不到它那儿。")
    sys.stdout.flush()
    print("\n  （下一句之后，我们只能靠**关掉管道写端**把它弄出来 ——")
    print("    也就是说：连「让它停止等待」都得靠外部拆管子，它自己等不到任何东西。）")
    sys.stdout.flush()

    sys.stdin = saved_stdin
    os.close(w)                 # 关掉写端 → 子线程的 read 才会结束
    stdin_pipe.close()

    print("\n  为什么会这样：**Python 的信号处理器只在主线程跑**。")
    print("  子线程里的阻塞调用（input / time.sleep / read）收不到 KeyboardInterrupt。")
    print("  在真实 CLI 里，主线程正要 join 线程池等结果 ——")
    print("  于是「用户想中断」变成「用户按了 Ctrl-C，然后还得再按一次才退出」。")
    print("  更糟的是我们精心做的「可中断 API 调用」（第 5 节）也一起失效了：")
    print("  中断事件能置位，但没人能停下来处理它。")


# ══════════════════════════════════════════════════════════════════
# 故障 3（更本质）：并发会让「答案 ↔ 问题」的对应关系丢失
# ══════════════════════════════════════════════════════════════════
def demo_lost_pairing() -> None:
    title("故障 3", "本质问题：回合制问答被并发打散，答案对不上问题")

    print("假设 3 个 interactive 工具并发跑，用户那边看到的是这样的：\n")
    for line in (
        "  [审批] terminal: 允许执行 rm -rf build ?  (y/N)",
        "  [审批] clarify : 你要用哪种数据库？          (输入)",
        "  [审批] terminal: 允许执行 sudo reboot ?   (y/N)",
    ):
        print(line)
        time.sleep(0.15)

    print("\n  用户敲一个 `y` —— 它**属于哪一条？**")
    print("  三条都在等输入，读到同一个 stdin，谁抢到算谁的。")
    print("  真实 Hermes 在 Telegram 上更清楚：用户会同时收到 3 条带按钮的消息，")
    print("  点哪个都像对的，但**配错一次，agent 就会带着错的假设继续跑下去**。")
    print("\n  → 所以 interactive 工具要的不是「串行执行」这个手段，")
    print("    而是**「一次只问一个问题」这个契约**。串行只是实现它的最简办法。")


if __name__ == "__main__":
    print("这个脚本会跑三个真实故障（约 4 秒）。")
    demo_output_interleave()
    demo_sigint_cannot_reach_worker()
    demo_lost_pairing()
    print(f"\n{BAR}")
    print("结论：interactive 工具**必须**强制整批工具退化为串行执行。")
    print("      代价是那一轮的工具变慢，收益是「问答」这个语义没有坏。")
    print(BAR)
