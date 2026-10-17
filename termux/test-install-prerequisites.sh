#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT
TEST_PREFIX="$TEST_ROOT/com.termux/files/usr"
FAKE_BIN="$TEST_ROOT/bin"
mkdir -p "$TEST_PREFIX" "$FAKE_BIN"
export TEST_PREREQ_ROOT="$TEST_ROOT"

# 模拟 curl 文件存在、却因旧动态库而无法运行。所有包操作均由测试替身处理。
cat >"$FAKE_BIN/curl" <<'EOF'
#!/usr/bin/env bash
[[ -f "$TEST_PREREQ_ROOT/ready" ]] || exit 127
[[ "$*" != "--version" ]] || exit 0
while (($#)); do
  if [[ "$1" == "-o" ]]; then
    printf 'touch "$TEST_PREREQ_ROOT/installer-started"\n' >"$2"
    exit 0
  fi
  shift
done
exit 98
EOF
cat >"$FAKE_BIN/apt" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$TEST_PREREQ_ROOT/apt.log"
case "$1" in
  update) exit 0 ;;
  full-upgrade)
    [[ "${TEST_UPGRADE_FAIL:-0}" == 0 ]] || exit 42
    [[ "${TEST_STILL_BROKEN:-0}" == 1 ]] || touch "$TEST_PREREQ_ROOT/ready"
    ;;
  install) exit 0 ;;
  *) exit 97 ;;
esac
EOF
cat >"$FAKE_BIN/proot-distro" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$TEST_PREREQ_ROOT/proot.log"
# 到达 Debian 检查即结束模拟；借用已有环境的保护分支避免创建任何环境。
[[ "${!#}" == true ]]
EOF
chmod 755 "$FAKE_BIN/"*

run_installer() {
  if PATH="$FAKE_BIN:$PATH" PREFIX="$TEST_PREFIX" \
    VERTEX_TERMUX_STATE_DIR="$TEST_ROOT/state" \
    bash "$ROOT_DIR/install-termux.sh" >"$TEST_ROOT/output" 2>&1; then
    printf '模拟安装应在 Debian 环境保护分支停止。\n' >&2
    exit 1
  fi
}

# 修复顺序：刷新索引、完整升级、安装依赖，之后才能进入 Debian 检查。
run_installer
cat >"$TEST_ROOT/expected" <<'EOF'
update
full-upgrade -y -o Dpkg::Options::=--force-confold
install -y curl proot-distro
EOF
diff -u "$TEST_ROOT/expected" "$TEST_ROOT/apt.log"
[[ -s "$TEST_ROOT/proot.log" ]]
grep -q '它不是本项目创建的' "$TEST_ROOT/output"

# 重跑且依赖正常时，不应再次进行包升级。
rm -f "$TEST_ROOT/apt.log" "$TEST_ROOT/proot.log"
run_installer
[[ ! -e "$TEST_ROOT/apt.log" && -s "$TEST_ROOT/proot.log" ]]

# 升级失败必须立即停止，不能继续安装依赖或启动 Debian。
rm -f "$TEST_ROOT/ready" "$TEST_ROOT/proot.log"
TEST_UPGRADE_FAIL=1 run_installer
[[ ! -e "$TEST_ROOT/proot.log" ]]
[[ "$(wc -l <"$TEST_ROOT/apt.log")" -eq 2 ]]
grep -q '安装失败' "$TEST_ROOT/output"

# 包管理器返回成功但 curl 仍坏时，必须给出恢复命令而非尝试下载。
rm -f "$TEST_ROOT/apt.log"
TEST_STILL_BROKEN=1 run_installer
[[ ! -e "$TEST_ROOT/proot.log" ]]
grep -q 'curl 仍无法运行' "$TEST_ROOT/output"
grep -q 'apt update && apt full-upgrade -y' "$TEST_ROOT/output"

# 执行 README 的真实安装入口，确保 curl 下载前就已完成依赖同步。
rm -f "$TEST_ROOT/ready" "$TEST_ROOT/apt.log"
mkdir -p "$TEST_ROOT/home"
mapfile -t install_commands < <(grep -F 'install-termux.sh?ref=main' "$ROOT_DIR/README.md")
[[ "${#install_commands[@]}" -eq 1 ]]
PATH="$FAKE_BIN:$PATH" HOME="$TEST_ROOT/home" bash -c "${install_commands[0]}"
diff -u "$TEST_ROOT/expected" "$TEST_ROOT/apt.log"
[[ -f "$TEST_ROOT/installer-started" ]]

printf 'Termux 依赖修复模拟检查通过。\n'
