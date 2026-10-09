"""mini 的网关（对应 Hermes 的 `gateway/`）。

布局也照真实版：一个**门面** `run.py`（`GatewayRunner`）+ 一组**兄弟模块**
`run_*.py`，每个管一个主题。

    真实 Hermes                       mini（本章）
    gateway/run.py（门面）      →     gateway/run.py
    gateway/run_*.py（21 个）   →     run_inbound.py / run_turn.py / run_shutdown.py
    gateway/session.py                gateway/session_keys.py
    gateway/authz_mixin.py            gateway/authz.py
    gateway/platforms/base.py         gateway/adapters/base.py
    gateway/status.py                 gateway/status.py
    gateway/hooks.py                  gateway/hooks.py
    gateway/delivery.py               gateway/delivery.py
    gateway/config_loader.py          gateway/profile_scope.py（只留作用域绑定那部分）

★ 为什么"门面 + 按主题拆的兄弟"值得学，而不是把网关写成一个 3000 行的类：
真实版的判据是"**一个文件过 ~2000 行 / 一个函数过 ~300 行就该按主题拆**"。
拆法不是按大小平均切，而是**按主题**切（inbound / turn / busy / shutdown…）——
这样"我该改哪个文件"有一个不看代码就能答的答案。
"""
