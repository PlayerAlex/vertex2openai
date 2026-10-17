#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_ROOT="${VERTEX_INSTALL_ROOT:-/opt/quick-vertex-api}"
REPO_DIR="${VERTEX_REPO_DIR:-$INSTALL_ROOT/repo}"
VENV_DIR="${VERTEX_VENV_DIR:-$INSTALL_ROOT/venv}"
STATE_DIR="${VERTEX_STATE_DIR:-/var/lib/quick-vertex-api}"
ENV_FILE="$STATE_DIR/service.env"
REQUIREMENTS_STAMP="$INSTALL_ROOT/requirements.sha256"
SYSTEM_STAMP="$INSTALL_ROOT/system-ready"
REPO_URL="${VERTEX_REPO_URL:-https://github.com/PlayerAlex/vertex2openai.git}"
BRANCH="${VERTEX_BRANCH:-main}"
PYTHON_BIN="${VERTEX_PYTHON_BIN:-python3}"

stage() {
  printf '\n==> %s\n' "$1"
}

die() {
  printf '错误：%s\n' "$1" >&2
  exit 1
}

as_root() {
  if [[ "${VERTEX_TEST_UNPRIVILEGED:-0}" == "1" ]]; then
    "$@"
  elif [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    die "需要在专用 Debian 容器内以 root 运行：$*"
  fi
}

write_default_env() {
  as_root mkdir -p "$STATE_DIR"
  as_root chmod 700 "$STATE_DIR"
  if [[ -f "$ENV_FILE" ]]; then
    return
  fi

  local api_key temp_file
  api_key="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  temp_file="$ENV_FILE.tmp.$$"
  umask 077
  {
    printf 'API_KEY=%q\n' "$api_key"
    printf 'CONSOLE_AUTH=local\n'
    printf 'STATE_DIR=%q\n' "$STATE_DIR"
    printf 'VERTEX_HOST=0.0.0.0\n'
    printf 'VERTEX_PORT=8050\n'
  } >"$temp_file"
  chmod 600 "$temp_file"
  mv -f "$temp_file" "$ENV_FILE"
}

install_system_packages() {
  [[ -f "$SYSTEM_STAMP" ]] && return
  stage "安装 Debian 内的 Python 与 Git"
  export DEBIAN_FRONTEND=noninteractive
  as_root apt-get update
  as_root apt-get install -y --no-install-recommends ca-certificates git python3 python3-pip python3-venv
  as_root apt-get clean
  as_root install -d -m 755 "$INSTALL_ROOT"
  as_root touch "$SYSTEM_STAMP"
}

sync_repository_initial() {
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    stage "下载快速vertex探测型api"
    as_root install -d -m 755 "$INSTALL_ROOT"
    git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$REPO_DIR"
    return
  fi

  [[ -z "$(git -C "$REPO_DIR" status --porcelain)" ]] || \
    die "源码目录存在本地修改，已停止更新。请先处理 $REPO_DIR 中的修改，再重试。"
  git -C "$REPO_DIR" fetch origin "$BRANCH"
  git -C "$REPO_DIR" merge --ff-only "origin/$BRANCH"
}

install_python_dependencies() {
  local requirements="$REPO_DIR/app/requirements.txt"
  [[ -f "$requirements" ]] || die "未找到依赖文件：$requirements"
  [[ -x "$VENV_DIR/bin/python" ]] || python3 -m venv "$VENV_DIR"

  local current_hash old_hash=""
  current_hash="$(sha256sum "$requirements" | awk '{print $1}')"
  [[ -f "$REQUIREMENTS_STAMP" ]] && old_hash="$(<"$REQUIREMENTS_STAMP")"
  if [[ "$current_hash" != "$old_hash" ]]; then
    stage "安装 Python 依赖（首次安装会花几分钟）"
    "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$requirements"
    printf '%s\n' "$current_hash" >"$REQUIREMENTS_STAMP"
  fi
}

bootstrap() {
  install_system_packages
  sync_repository_initial
  install_python_dependencies
  write_default_env
}

update_repo() {
  [[ -d "$REPO_DIR/.git" ]] || die "尚未完成安装，请重新运行一键安装命令。"
  [[ -z "$(git -C "$REPO_DIR" status --porcelain)" ]] || \
    die "检测到源码本地修改，为避免覆盖已取消更新。请先提交或移走这些修改。"

  stage "检查上游更新"
  git -C "$REPO_DIR" fetch origin "$BRANCH"
  git -C "$REPO_DIR" merge --ff-only "origin/$BRANCH"
  install_python_dependencies
}

serve() {
  [[ -x "$VENV_DIR/bin/python" ]] || die "Python 环境不存在，请重新运行安装命令。"
  [[ -f "$ENV_FILE" ]] || die "服务配置不存在，请重新运行安装命令。"
  set -a
  # 此文件由安装器以 0600 权限生成，允许用户按需编辑高级环境变量。
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  cd "$REPO_DIR"
  exec "$VENV_DIR/bin/python" -m uvicorn main:app \
    --app-dir app --host "${VERTEX_HOST:-0.0.0.0}" --port "${VERTEX_PORT:-8050}"
}

case "${1:-}" in
  bootstrap) bootstrap ;;
  init-config) write_default_env ;;
  update) update_repo ;;
  serve) serve ;;
  *) die "未知内部命令：${1:-空}" ;;
esac
