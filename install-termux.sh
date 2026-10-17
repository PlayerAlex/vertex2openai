#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail

VERTEX_DEFAULT_DISTRO=quickvertex
VERTEX_DEFAULT_STATE_ROOT="$HOME/.quick-vertex-api"
if [[ -f "${PREFIX:-/nonexistent}/etc/quick-vertex.conf" ]]; then
  . "$PREFIX/etc/quick-vertex.conf"
fi
DISTRO="${VERTEX_TERMUX_DISTRO:-$VERTEX_DEFAULT_DISTRO}"
STATE_ROOT="${VERTEX_TERMUX_STATE_DIR:-$VERTEX_DEFAULT_STATE_ROOT}"
GUEST_STATE="/var/lib/quick-vertex-api"
REPO_URL="${VERTEX_REPO_URL:-https://github.com/PlayerAlex/vertex2openai.git}"
BRANCH="${VERTEX_BRANCH:-main}"
RAW_BASE="${VERTEX_RAW_BASE:-https://raw.githubusercontent.com/PlayerAlex/vertex2openai/main}"
TEMP_DIR=""

stage() {
  printf '\n\033[1;36m[%s/5] %s\033[0m\n' "$1" "$2"
}

die() {
  printf '\n安装失败：%s\n' "$1" >&2
  printf '可以修复网络或存储空间后，重新运行同一条安装命令；已有 API Key 不会轮换。\n' >&2
  exit 1
}

cleanup() {
  [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]] && rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT
trap 'die "命令在第 $LINENO 行执行失败。"' ERR

[[ -n "${PREFIX:-}" && "$PREFIX" == */com.termux/* ]] || \
  die "请在 Android 的 Termux 中运行此脚本。"
command -v pkg >/dev/null 2>&1 || die "未找到 Termux 的 pkg 命令。"

stage 1 "安装缺少的基础工具"
packages=()
command -v curl >/dev/null 2>&1 || packages+=(curl)
command -v proot-distro >/dev/null 2>&1 || packages+=(proot-distro)
if ((${#packages[@]})); then
  pkg update -y
  pkg install -y "${packages[@]}"
fi
command -v curl >/dev/null 2>&1 || die "curl 安装失败。"
command -v proot-distro >/dev/null 2>&1 || die "proot-distro 安装失败。"

stage 2 "准备专用 Debian 环境（不会覆盖已有 Debian）"
if proot-distro login "$DISTRO" -- true >/dev/null 2>&1; then
  if ! proot-distro login "$DISTRO" -- test -f /etc/quick-vertex-api-owner; then
    die "已存在名为 $DISTRO 的环境，但它不是本项目创建的。为保护数据，请设置 VERTEX_TERMUX_DISTRO 为另一个名称后重试。"
  fi
else
  install_help="$(proot-distro install --help 2>&1)"
  if grep -q -- '--name' <<<"$install_help"; then
    proot-distro install debian --name "$DISTRO"
  else
    proot-distro install debian --override-alias "$DISTRO"
  fi
  proot-distro login "$DISTRO" -- sh -c \
    'umask 022; touch /etc/quick-vertex-api-owner; mkdir -p /var/lib/quick-vertex-api'
fi

stage 3 "下载并安装服务"
mkdir -p "$STATE_ROOT"
chmod 700 "$STATE_ROOT"
if [[ -f "$PREFIX/bin/vertex" ]]; then
  grep -q 'Quick Vertex managed launcher' "$PREFIX/bin/vertex" || \
    die "已有其他 vertex 命令，请先为它改名，安装器不会覆盖它。"
  "$PREFIX/bin/vertex" stop
fi
TEMP_DIR="$(mktemp -d "${TMPDIR:-$PREFIX/tmp}/quick-vertex.XXXXXX")"
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
  "$RAW_BASE/termux/vertex-internal.sh" -o "$TEMP_DIR/vertex-internal.sh"
chmod 755 "$TEMP_DIR/vertex-internal.sh"
proot-distro login \
  --bind "$STATE_ROOT:$GUEST_STATE" \
  --bind "$TEMP_DIR/vertex-internal.sh:/tmp/vertex-internal.sh" \
  "$DISTRO" -- env \
  VERTEX_REPO_URL="$REPO_URL" VERTEX_BRANCH="$BRANCH" \
  bash /tmp/vertex-internal.sh bootstrap

stage 4 "安装 vertex 管理命令"
mkdir -p "$PREFIX/etc"
config_temp="$PREFIX/etc/quick-vertex.conf.tmp.$$"
printf 'VERTEX_DEFAULT_DISTRO=%q\nVERTEX_DEFAULT_STATE_ROOT=%q\n' "$DISTRO" "$STATE_ROOT" >"$config_temp"
chmod 600 "$config_temp"
mv -f "$config_temp" "$PREFIX/etc/quick-vertex.conf"
manager_temp="$PREFIX/bin/vertex.new.$$"
proot-distro login --bind "$STATE_ROOT:$GUEST_STATE" "$DISTRO" -- \
  cat /opt/quick-vertex-api/repo/termux/vertex >"$manager_temp"
chmod 755 "$manager_temp"
mv -f "$manager_temp" "$PREFIX/bin/vertex"

stage 5 "启动并检查服务"
trap - ERR
if ! vertex start; then
  die "服务未能启动。请运行 vertex logs 查看详细原因，然后运行 vertex start 重试。"
fi

printf '\n安装完成。以后只需输入 vertex 查看管理命令。\n'
printf 'API 地址：%s\n' 'http://127.0.0.1:8050/v1'
printf 'Google Key 可在刚打开的控制台中填写，不需要写进安装命令。\n'
