---
name: mini-repo-conventions
description: 本仓（mini-hermes）工程约定：改完跑对应测试、双解释器、清 __pycache__、零依赖。
---

# mini-hermes 工程约定

本仓（mini-hermes）的工程约定。改动代码前后照着做，能避开大部分
"本地绿、换台机器红"的坑。

## 1. 零第三方依赖（硬约束）

运行时代码**只用标准库**，不引入任何第三方包。
`tests/test_packaging.py` 用 AST 逐文件扫 import，一旦混进第三方库立刻红。
新功能需要某个库时：先想标准库能不能做，再想是不是该把这个功能砍掉 ——
不要偷偷加依赖（README 与 requirements.txt 还写着"零依赖"，那种不一致更糟）。

## 2. 改完就跑**对应**的测试

每种改动都归属某个 `tests/test_*.py`（仓库根 `AGENTS.md` 的
"改动落在哪、要跑哪些测试"映射表：改哪儿 → 跑哪个文件）。
至少跑**受影响的那几个**，别只跑一个：

```bash
python3 tests/test_prompt_assembly.py   # 提示词装配 / 字节稳定
python3 tests/test_tools_runtime.py     # 工具运行时 / toolset 闸门
python3 tests/test_skills.py            # 技能发现与渐进披露
```

## 3. 两个解释器都要跑

至少在这两个上都跑通（本项目都用它们实测过整套）：

```bash
/usr/local/bin/python3 tests/test_skills.py   # Python 3.13
/usr/bin/python3        tests/test_skills.py   # Python 3.9（系统自带）
```

版本差异（例如某些 SQLite / FTS5 分支）往往只在其中一个上暴露。
**只跑一个解释器的结果不算数。**

## 4. 跑前先清 `__pycache__`

```bash
find . -name __pycache__ -type d -exec rm -rf {} +
```

`.pyc` 的失效判据是 `(mtime, 文件大小)`。"两块对调"这类编辑文件长度不变、
mtime 又落在同一秒 → 缓存被判新鲜，**实际跑的是旧字节码**。
改动后结果"看着没变"时，先清缓存再跑一遍。

## 5. 新增 / 删除模块 ⇒ 一定跑 `test_packaging.py`

```bash
python3 tests/test_packaging.py
```

它守"别人 clone 下来能跑"：零依赖 · 无硬编码绝对路径 · 启动脚本能被符号链接调用 ·
命令行旗标与 README 同步。**加了新模块却不跑它 = 发布纪律破了个口子。**
