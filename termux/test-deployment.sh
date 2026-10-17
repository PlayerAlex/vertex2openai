#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_ROOT"' EXIT

bash -n "$ROOT_DIR/install-termux.sh"
bash -n "$ROOT_DIR/termux/vertex"
bash -n "$ROOT_DIR/termux/vertex-internal.sh"

SOURCE_REPO="$TEMP_ROOT/source"
INSTALL_ROOT="$TEMP_ROOT/install"
STATE_DIR="$TEMP_ROOT/state"
mkdir -p "$SOURCE_REPO/app" "$INSTALL_ROOT" "$STATE_DIR"
: >"$SOURCE_REPO/app/requirements.txt"
git -C "$SOURCE_REPO" init -b main -q
git -C "$SOURCE_REPO" config user.name termux-test
git -C "$SOURCE_REPO" config user.email termux-test@example.invalid
git -C "$SOURCE_REPO" add app/requirements.txt
git -C "$SOURCE_REPO" commit -qm initial
touch "$INSTALL_ROOT/system-ready"

run_internal() {
  VERTEX_INSTALL_ROOT="$INSTALL_ROOT" \
  VERTEX_STATE_DIR="$STATE_DIR" \
  VERTEX_REPO_URL="$SOURCE_REPO" \
  VERTEX_BRANCH=main \
  VERTEX_TEST_UNPRIVILEGED=1 \
    bash "$ROOT_DIR/termux/vertex-internal.sh" "$@"
}

if [[ "$(uname -s)" == MINGW* ]]; then
  # Git Bash on Windows cannot create a POSIX venv. The Linux CI branch below
  # performs the complete isolated bootstrap; locally we still verify state logic.
  export VERTEX_PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
  run_internal init-config
else
  run_internal bootstrap
fi
first_key="$(sed -n 's/^API_KEY=//p' "$STATE_DIR/service.env")"
[[ -n "$first_key" ]]
if [[ "$(uname -s)" != MINGW* ]]; then
  [[ "$(stat -c '%a' "$STATE_DIR/service.env")" == "600" ]]
fi

# 重跑模拟：源码、venv 和状态都已存在，API Key 必须保持不变。
if [[ "$(uname -s)" == MINGW* ]]; then
  run_internal init-config
else
  run_internal bootstrap
fi
second_key="$(sed -n 's/^API_KEY=//p' "$STATE_DIR/service.env")"
[[ "$first_key" == "$second_key" ]]

# 失败路径：本地源码有修改时，更新必须失败且不能覆盖文件或轮换密钥。
if [[ "$(uname -s)" == MINGW* ]]; then
  mkdir -p "$INSTALL_ROOT/repo/.git"
fi
printf 'local change\n' >"$INSTALL_ROOT/repo/local-change.txt"
if run_internal update >/dev/null 2>&1; then
  printf '脏源码更新本应失败，但命令返回成功。\n' >&2
  exit 1
fi
[[ -f "$INSTALL_ROOT/repo/local-change.txt" ]]
[[ "$first_key" == "$(sed -n 's/^API_KEY=//p' "$STATE_DIR/service.env")" ]]

if run_internal unknown-command >/dev/null 2>&1; then
  printf '未知内部命令本应失败，但命令返回成功。\n' >&2
  exit 1
fi

# 管理命令模拟：已有 /healthz 的占用者也不能被当成本服务；复用 PID 不能被误杀。
FAKE_BIN="$TEMP_ROOT/fake-bin"
MANAGER_STATE="$TEMP_ROOT/manager-state"
mkdir -p "$FAKE_BIN" "$MANAGER_STATE"
cat >"$FAKE_BIN/curl" <<'EOF'
#!/usr/bin/env bash
exit "${FAKE_CURL_RESULT:-1}"
EOF
cat >"$FAKE_BIN/proot-distro" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_PROOT_LOG:?}"
exit 99
EOF
chmod 755 "$FAKE_BIN/curl" "$FAKE_BIN/proot-distro"

if PATH="$FAKE_BIN:$PATH" FAKE_CURL_RESULT=0 FAKE_PROOT_LOG="$TEMP_ROOT/proot.log" \
  VERTEX_TERMUX_STATE_DIR="$MANAGER_STATE" PREFIX="$TEMP_ROOT/prefix" \
  bash "$ROOT_DIR/termux/vertex" start >/dev/null 2>&1; then
  printf '端口被其他健康服务占用时，start 本应拒绝启动。\n' >&2
  exit 1
fi
[[ ! -s "$TEMP_ROOT/proot.log" ]]

sleep 30 &
unrelated_pid=$!
printf '%s\n' "$unrelated_pid" >"$MANAGER_STATE/service.pid"
PATH="$FAKE_BIN:$PATH" FAKE_CURL_RESULT=1 FAKE_PROOT_LOG="$TEMP_ROOT/proot.log" \
  VERTEX_TERMUX_STATE_DIR="$MANAGER_STATE" PREFIX="$TEMP_ROOT/prefix" \
  bash "$ROOT_DIR/termux/vertex" stop >/dev/null
kill -0 "$unrelated_pid"
kill "$unrelated_pid"
wait "$unrelated_pid" 2>/dev/null || true

if [[ "$(uname -s)" != MINGW* ]]; then
  # login 会 exec 成 proot；实际 /proc 信息应识别新进程名，并拒绝旧启动时间。
  bash -c 'while :; do sleep 1; done' proot \
    "$MANAGER_STATE:/var/lib/quick-vertex-api" \
    /opt/quick-vertex-api/repo/termux/vertex-internal.sh serve &
  managed_pid=$!
  trap 'kill "$managed_pid" 2>/dev/null || true; rm -rf -- "$TEMP_ROOT"' EXIT
  started="$(awk '{print $22}' "/proc/$managed_pid/stat")"
  printf '%s %s\n' "$managed_pid" "$started" >"$MANAGER_STATE/service.pid"
  PATH="$FAKE_BIN:$PATH" FAKE_CURL_RESULT=0 VERTEX_TERMUX_STATE_DIR="$MANAGER_STATE" \
    bash "$ROOT_DIR/termux/vertex" status
  printf '%s 0\n' "$managed_pid" >"$MANAGER_STATE/service.pid"
  PATH="$FAKE_BIN:$PATH" FAKE_CURL_RESULT=1 VERTEX_TERMUX_STATE_DIR="$MANAGER_STATE" \
    bash "$ROOT_DIR/termux/vertex" stop >/dev/null
  kill -0 "$managed_pid"
  kill "$managed_pid"
  wait "$managed_pid" 2>/dev/null || true
  trap 'rm -rf -- "$TEMP_ROOT"' EXIT
fi

if grep -Eq '(^|[^[:alnum:]_-])(pkill|killall)([^[:alnum:]_-]|$)' "$ROOT_DIR/termux/vertex"; then
  printf '管理命令不应宽泛终止进程。\n' >&2
  exit 1
fi

grep -q 'quickvertex' "$ROOT_DIR/install-termux.sh"
grep -q 'pkg install -y' "$ROOT_DIR/install-termux.sh"
if grep -Eq 'pkg +(upgrade|update.*&&.*upgrade)' "$ROOT_DIR/install-termux.sh"; then
  printf '安装器不应执行全系统升级。\n' >&2
  exit 1
fi

printf 'Termux 部署脚本模拟检查通过。\n'
