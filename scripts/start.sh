#!/usr/bin/env bash

# Start the local quantitative monitor from Git Bash, WSL, or a POSIX shell.
# Windows Bash environments delegate to start.ps1 so they share the one-time
# DPAPI credential setup with start.cmd. Native POSIX systems inherit secrets
# from the caller and never try to read a Windows credential.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd -- "$PROJECT_ROOT"

PLATFORM="$(uname -s 2>/dev/null || printf 'unknown')"

# Parse the provider here as well as in the Windows launcher. Native POSIX and
# WSL-without-interoperability must not forward an unsupported --provider flag
# to the Python CLI. A provider from the environment is an intentional
# override; otherwise a custom JSON profile remains authoritative.
SELECTED_PROVIDER="${QUANT_MONITOR_DATA_PROVIDER:-asharehub}"
PROVIDER_OVERRIDE=0
if [[ -n "${QUANT_MONITOR_DATA_PROVIDER:-}" ]]; then
  SELECTED_PROVIDER="${QUANT_MONITOR_DATA_PROVIDER,,}"
  PROVIDER_OVERRIDE=1
fi
FORWARD_ARGS=()
CUSTOM_CONFIG_SUPPLIED=0
CONFIG_PATH=""
if [[ -n "${QUANT_MONITOR_CONFIG_PATH:-}" ]]; then
  CUSTOM_CONFIG_SUPPLIED=1
  CONFIG_PATH="$QUANT_MONITOR_CONFIG_PATH"
fi
while (($#)); do
  case "$1" in
    --provider|-Provider)
      if (($# < 2)); then
        printf '错误：--provider 需要 asharehub、alltick、akshare 或 mock。\n' >&2
        exit 2
      fi
      SELECTED_PROVIDER="${2,,}"
      PROVIDER_OVERRIDE=1
      shift 2
      ;;
    --config)
      if (($# < 2)); then
        printf '错误：--config 需要 JSON 配置文件路径。\n' >&2
        exit 2
      fi
      CUSTOM_CONFIG_SUPPLIED=1
      CONFIG_PATH="$2"
      FORWARD_ARGS+=("$1" "$2")
      shift 2
      ;;
    --config=*)
      CUSTOM_CONFIG_SUPPLIED=1
      CONFIG_PATH="${1#--config=}"
      FORWARD_ARGS+=("$1")
      shift
      ;;
    *)
      FORWARD_ARGS+=("$1")
      shift
      ;;
  esac
done

validate_provider() {
  case "$SELECTED_PROVIDER" in
    asharehub|alltick|akshare|mock) ;;
    *)
      printf '错误：不支持的数据源：%s。\n' "$SELECTED_PROVIDER" >&2
      exit 2
      ;;
  esac
}

# Windows Bash delegates config resolution to start.ps1. Native POSIX/WSL
# fallback resolves it after the venv Python is available (JSON parsing with a
# regex would be unsafe and would mishandle quoted/escaped values).
if [[ "$PROVIDER_OVERRIDE" -eq 1 ]]; then
  validate_provider
fi

run_windows_launcher() {
  local windows_script
  if ! command -v powershell.exe >/dev/null 2>&1; then
    return 126
  fi
  if command -v cygpath >/dev/null 2>&1; then
    windows_script="$(cygpath -w "$SCRIPT_DIR/start.ps1")"
  elif command -v wslpath >/dev/null 2>&1; then
    windows_script="$(wslpath -w "$SCRIPT_DIR/start.ps1")"
  else
    return 126
  fi
  # WSL may expose a powershell.exe path even when Windows interoperability is
  # disabled. Probe execution before handing control to the Windows launcher so
  # native Linux Python remains a usable fallback in that configuration.
  if ! powershell.exe -NoLogo -NoProfile -Command 'exit 0' >/dev/null 2>&1; then
    return 126
  fi
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass \
    -File "$windows_script" "$@"
}

case "$PLATFORM" in
  MINGW*|MSYS*|CYGWIN*)
    if [[ "$PROVIDER_OVERRIDE" -eq 1 ]]; then
      if run_windows_launcher --provider "$SELECTED_PROVIDER" "${FORWARD_ARGS[@]}"; then
        exit 0
      else
        status=$?
      fi
    else
      if run_windows_launcher "${FORWARD_ARGS[@]}"; then
        exit 0
      else
        status=$?
      fi
    fi
    if [[ "$status" -eq 126 ]]; then
      printf '错误：未找到可执行的 Windows PowerShell。\n' >&2
    fi
    exit "$status"
    ;;
  Linux*)
    if [[ -r /proc/sys/kernel/osrelease ]] && \
      grep -qi microsoft /proc/sys/kernel/osrelease; then
      if [[ "$PROVIDER_OVERRIDE" -eq 1 ]]; then
        if run_windows_launcher --provider "$SELECTED_PROVIDER" "${FORWARD_ARGS[@]}"; then
          exit 0
        else
          status=$?
        fi
      else
        if run_windows_launcher "${FORWARD_ARGS[@]}"; then
          exit 0
        else
          status=$?
        fi
      fi
      if [[ "$status" -ne 126 ]]; then
        exit "$status"
      fi
      printf '提示：Windows 互操作不可用，改用 WSL 本地 Python 启动。\n' >&2
    fi
    ;;
esac

case "$PLATFORM" in
  MINGW*|MSYS*|CYGWIN*)
    DEFAULT_VENV="$PROJECT_ROOT/.venv"
    ;;
  *)
    # A Windows venv cannot be imported by Linux/WSL Python. Keep a separate
    # POSIX environment when the repository already contains .venv/Scripts.
    if [[ -f "$PROJECT_ROOT/.venv/Scripts/python.exe" && ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
      DEFAULT_VENV="$PROJECT_ROOT/.venv-posix"
    else
      DEFAULT_VENV="$PROJECT_ROOT/.venv"
    fi
    ;;
esac

VENV_DIR="${QUANT_MONITOR_VENV_DIR:-$DEFAULT_VENV}"

find_venv_python() {
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    printf '%s\n' "$VENV_DIR/bin/python"
    return 0
  fi
  if [[ -f "$VENV_DIR/Scripts/python.exe" ]]; then
    printf '%s\n' "$VENV_DIR/Scripts/python.exe"
    return 0
  fi
  return 1
}

find_bootstrap_python() {
  local candidate path
  for candidate in python3 python; do
    if path="$(command -v "$candidate" 2>/dev/null)" && \
      "$path" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}

if ! PYTHON="$(find_venv_python)"; then
  if ! BOOTSTRAP_PYTHON="$(find_bootstrap_python)"; then
    printf '错误：未找到 Python。请先安装 Python 3.9 或更高版本。\n' >&2
    exit 1
  fi
  printf '正在创建虚拟环境：%s\n' "$VENV_DIR"
  if ! "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"; then
    printf '错误：无法创建虚拟环境；Debian/Ubuntu/WSL 请确认已安装 python3-venv。\n' >&2
    exit 1
  fi
  PYTHON="$(find_venv_python)"
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  printf '错误：虚拟环境需要 Python 3.9 或更高版本：%s\n' "$PYTHON" >&2
  exit 1
fi

# A native POSIX/WSL fallback has no PowerShell to inspect a custom profile.
# Resolve its provider with the same JSON parser used by the application. The
# result is used only for dependency/credential routing; Python still loads the
# profile itself and remains the final configuration authority.
if [[ "$PROVIDER_OVERRIDE" -eq 0 && "$CUSTOM_CONFIG_SUPPLIED" -eq 1 ]]; then
  if [[ -z "$CONFIG_PATH" ]]; then
    printf '错误：--config 需要 JSON 配置文件路径。\n' >&2
    exit 2
  fi
  if ! CONFIG_PROVIDER="$($PYTHON -c 'import json,sys; from pathlib import Path; payload=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); value=payload.get("data_provider") if isinstance(payload,dict) else None; print(str(value or "asharehub").strip().lower())' "$CONFIG_PATH" 2>/dev/null)"; then
    printf '错误：配置文件不是有效的 JSON：%s\n' "$CONFIG_PATH" >&2
    exit 2
  fi
  SELECTED_PROVIDER="${CONFIG_PROVIDER,,}"
fi
validate_provider

# Keep the readiness hint aligned with the port forwarded to the Python CLI.
# This is display-only; the CLI remains the source of truth for argument
# validation and binding.
DISPLAY_PORT="8000"
for ((index = 0; index < ${#FORWARD_ARGS[@]}; index++)); do
  case "${FORWARD_ARGS[$index]}" in
    --port)
      if ((index + 1 < ${#FORWARD_ARGS[@]})); then
        DISPLAY_PORT="${FORWARD_ARGS[$((index + 1))]}"
      fi
      ;;
    --port=*)
      DISPLAY_PORT="${FORWARD_ARGS[$index]#--port=}"
      ;;
  esac
done

if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
  "$PYTHON" -m ensurepip --upgrade
fi

DEPENDENCY_PROBE='import fastapi, uvicorn'
if [[ "$SELECTED_PROVIDER" == "akshare" || "$SELECTED_PROVIDER" == "alltick" ]]; then
  DEPENDENCY_PROBE='import fastapi, uvicorn, akshare, websockets'
fi
if ! "$PYTHON" -c "$DEPENDENCY_PROBE" >/dev/null 2>&1; then
  printf '正在安装运行依赖……\n'
  "$PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt"
fi

export PYTHONUTF8="${PYTHONUTF8:-1}"
if [[ "$PROVIDER_OVERRIDE" -eq 1 ]]; then
  export QUANT_MONITOR_DATA_PROVIDER="$SELECTED_PROVIDER"
else
  unset QUANT_MONITOR_DATA_PROVIDER
fi
# Until source is part of every persistence key, provider isolation is a data
# correctness boundary. Respect an explicit path but choose a safe per-provider
# default for POSIX/native-WSL launches.
if [[ "$CUSTOM_CONFIG_SUPPLIED" -eq 0 && -z "${QUANT_MONITOR_DATABASE_PATH:-}" && -z "${QUANT_MONITOR_DB_PATH:-}" ]]; then
  if [[ "$PROVIDER_OVERRIDE" -eq 1 ]]; then
    export QUANT_MONITOR_DATABASE_PATH="$PROJECT_ROOT/data/quant_monitor_${QUANT_MONITOR_DATA_PROVIDER}.db"
  else
    export QUANT_MONITOR_DATABASE_PATH="$PROJECT_ROOT/data/quant_monitor_${SELECTED_PROVIDER}.db"
  fi
fi
if [[ "$SELECTED_PROVIDER" == "asharehub" && -z "${QUANT_MONITOR_ASHAREHUB_API_KEY:-}" ]]; then
  printf '错误：请先设置 QUANT_MONITOR_ASHAREHUB_API_KEY，或在 Windows 上直接运行 scripts\\start.cmd 完成加密录入。\n' >&2
  exit 1
fi
printf "提示：服务会常驻当前终端；看到 'Application startup complete' 即启动完成，可访问 http://127.0.0.1:%s/ready 检查，按 Ctrl+C 停止。\n" "$DISPLAY_PORT"
exec "$PYTHON" -m quant_monitor "${FORWARD_ARGS[@]}"
