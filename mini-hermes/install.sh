#!/usr/bin/env bash
# 把 mini 装成一个**全局命令**：装完之后，在任意目录敲 `mini` 就能用。
#
#   · 工作区 = **你敲命令时所在的目录**（启动器把调用目录记在 MINI_LAUNCH_CWD，
#     所以你在哪个项目里敲 `mini`，agent 的工作区就是那个项目，而不是 mini 自己的目录）。
#   · 启动器**自己会解开符号链接**（绝对 / 相对 / 链的链都行），所以这里只建链接、不改代码。
#     这条能力有契约测试守着：`tests/test_packaging.py::test_launcher_via_symlink`。
#
# 用法：
#   ./install.sh                    # 默认装到 ~/.local/bin
#   ./install.sh /usr/local/bin     # 或指定目录（Homebrew 用户通常可直接写）
#   MINI_BIN_DIR=~/bin ./install.sh # 也可以用环境变量
#
# 卸载：删掉那个链接（例如 `rm ~/.local/bin/mini`）。它只建链接，不写别的东西。
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="$here/mini"
bin_dir="${1:-${MINI_BIN_DIR:-$HOME/.local/bin}}"

if [ ! -x "$target" ]; then
    echo "✗ 找不到可执行的启动器：$target" >&2
    exit 1
fi

mkdir -p "$bin_dir"
ln -sfn "$target" "$bin_dir/mini"
echo "✓ $bin_dir/mini → $target"

# PATH 提示：装完敲不出来，九成是这个原因（而不是装失败）
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
        echo "⚠️  $bin_dir 不在 PATH 里。把下面这行加进你的 shell 配置（~/.zshrc 或 ~/.bashrc）："
        echo "      export PATH=\"$bin_dir:\$PATH\""
        ;;
esac

# 自检：真跑一次（不需要密钥、不联网）
if "$bin_dir/mini" --list-toolsets >/dev/null 2>&1; then
    echo "✓ 自检通过：$bin_dir/mini --list-toolsets 跑得起来"
else
    echo "✗ 自检失败 —— 手动跑一次看报错：$bin_dir/mini --list-toolsets" >&2
    exit 1
fi
