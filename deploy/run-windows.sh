#!/usr/bin/env bash
# Helix — clean Windows bootstrap + run (Git Bash / MSYS2)
#
# Usage (from repo root or this folder):
#   bash deploy/run-windows.sh
#   bash deploy/run-windows.sh --fresh     # always wipe .venv and recreate
#   bash deploy/run-windows.sh --no-run    # setup only, do not start the app
#
# Requirements:
#   - Windows (Git Bash recommended)
#   - Python 3.14 via the Windows launcher:  py -3.14 --version

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

PY_VER="3.14"
VENV_DIR="${ROOT}/.venv"
VENV_PY="${VENV_DIR}/Scripts/python.exe"
FRESH=0
DO_RUN=1

for arg in "$@"; do
  case "${arg}" in
    --fresh|-f) FRESH=1 ;;
    --no-run)   DO_RUN=0 ;;
    --help|-h)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: ${arg}" >&2
      echo "Use --help for usage." >&2
      exit 2
      ;;
  esac
done

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- Windows host check (Git Bash / MSYS / Cygwin / native uname) ---
is_windows_host() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*|Windows_NT*) return 0 ;;
  esac
  [[ "${OS:-}" == "Windows_NT" ]] && return 0
  [[ -n "${WINDIR:-}" || -n "${SYSTEMROOT:-}" ]] && return 0
  return 1
}

is_windows_host || die "This script is for Windows only (run under Git Bash on Windows)."

# --- Resolve Python 3.14 (Windows py launcher preferred) ---
resolve_python314() {
  if command -v py >/dev/null 2>&1; then
    if py -3.14 -c "import sys; assert sys.version_info[:2]==(3,14)" >/dev/null 2>&1; then
      echo "py -3.14"
      return 0
    fi
  fi
  for cand in python3.14 python314 python; do
    if command -v "${cand}" >/dev/null 2>&1; then
      if "${cand}" -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,14) else 1)" >/dev/null 2>&1; then
        echo "${cand}"
        return 0
      fi
    fi
  done
  return 1
}

# Run a command that may be "py -3.14" (two tokens) or a single binary
run_py() {
  # shellcheck disable=SC2086
  local launcher="$1"
  shift
  if [[ "${launcher}" == "py -3.14" ]]; then
    py -3.14 "$@"
  else
    "${launcher}" "$@"
  fi
}

venv_is_windows() {
  [[ -f "${VENV_PY}" ]]
}

# --- 1) Clear bytecode caches ---
log "Clearing __pycache__ and *.pyc under ${ROOT}"
find "${ROOT}" \
  \( -path "${ROOT}/.venv" -o -path "${ROOT}/.git" -o -path "${ROOT}/node_modules" \) -prune -o \
  \( -type d -name '__pycache__' -print \) 2>/dev/null \
  | while IFS= read -r d; do
      rm -rf "${d}"
    done
find "${ROOT}" \
  \( -path "${ROOT}/.venv" -o -path "${ROOT}/.git" \) -prune -o \
  \( -type f \( -name '*.pyc' -o -name '*.pyo' \) -print \) 2>/dev/null \
  | while IFS= read -r f; do
      rm -f "${f}"
    done
# Also drop Windows venv bytecode if present
if [[ -d "${VENV_DIR}" ]]; then
  find "${VENV_DIR}" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
fi
echo "Caches cleared."

# --- 2) Drop non-Windows (or forced) venv ---
if [[ "${FRESH}" -eq 1 && -e "${VENV_DIR}" ]]; then
  log "Removing .venv (--fresh)"
  rm -rf "${VENV_DIR}"
elif [[ -e "${VENV_DIR}" ]] && ! venv_is_windows; then
  log "Existing .venv is not a Windows venv (missing Scripts/python.exe) — removing"
  # Common when a macOS/Linux .venv was copied onto Windows
  if [[ -d "${VENV_DIR}/bin" && ! -d "${VENV_DIR}/Scripts" ]]; then
    warn "Detected Unix-style .venv/bin — recreating for Windows."
  fi
  rm -rf "${VENV_DIR}"
elif venv_is_windows; then
  log "Keeping existing Windows .venv"
fi

# --- 3) Create Windows .venv with Python 3.14 ---
PY314="$(resolve_python314)" || die "Python ${PY_VER} not found. Install it, then verify:  py -3.14 --version"
log "Using interpreter: ${PY314}"
run_py "${PY314}" -c "import sys; print(sys.version)"

if [[ ! -f "${VENV_PY}" ]]; then
  log "Creating Windows venv at .venv (Python ${PY_VER})"
  run_py "${PY314}" -m venv "${VENV_DIR}"
  venv_is_windows || die "venv created but ${VENV_PY} is missing"
fi

# Prefer venv python for everything below
PY="${VENV_PY}"
# Git Bash path → still works; also allow win path via cygpath if available
if command -v cygpath >/dev/null 2>&1; then
  PY_WIN="$(cygpath -w "${VENV_PY}")"
else
  PY_WIN="${VENV_PY}"
fi

log "Venv Python: ${PY}"
"${PY}" -c "import sys; print(sys.executable); print(sys.version); assert sys.version_info[:2]==(3,14), 'venv is not Python 3.14'"

# --- 4) Point Helix jobs at Windows venv python ---
SCRIPTS_JSON="${ROOT}/config/scripts.json"
if [[ -f "${SCRIPTS_JSON}" ]]; then
  log "Setting config/scripts.json python → .venv\\\\Scripts\\\\python.exe"
  "${PY}" - <<'PY'
from pathlib import Path
import re
path = Path("config/scripts.json")
text = path.read_text(encoding="utf-8")
# JSON value uses doubled backslashes: .venv\\Scripts\\python.exe
replacement = '"python": ".venv\\\\Scripts\\\\python.exe"'
new_text, n = re.subn(r'"python"\s*:\s*"[^"]*"', replacement, text, count=1)
if n != 1:
    raise SystemExit(f"could not patch python path in {path} (matches={n})")
if new_text != text:
    path.write_text(new_text, encoding="utf-8")
    print("updated", path)
else:
    print("already set", path)
PY
fi

# --- 5) .env ---
if [[ ! -f "${ROOT}/.env" && -f "${ROOT}/.env.example" ]]; then
  log "Creating .env from .env.example"
  cp "${ROOT}/.env.example" "${ROOT}/.env"
  warn "Edit .env (HUB_SECRET_KEY / passwords) before sharing this host."
fi

# --- 6) Dependencies ---
log "Upgrading pip / wheel"
"${PY}" -m pip install --upgrade pip setuptools wheel

log "Installing requirements.txt"
if ! "${PY}" -m pip install -r "${ROOT}/requirements.txt"; then
  warn "Full requirements install failed (common on 3.14 for PaddleOCR wheels)."
  warn "Installing core Helix web deps so the app can still start…"
  "${PY}" -m pip install "flask>=3.0.0" "jinja2>=3.1.0" "jsonschema>=4.22.0" \
    || die "Could not install core dependencies."
fi

log "Playwright Chromium (PNR) — best effort"
"${PY}" -m playwright install chromium || warn "Playwright browser install skipped/failed (PNR may not work until fixed)."

# --- 7) Run ---
if [[ "${DO_RUN}" -eq 0 ]]; then
  log "Setup complete (--no-run). Start later with:"
  echo "  \"${PY}\" app.py"
  exit 0
fi

log "Starting Helix → http://127.0.0.1:5050"
echo "    python: ${PY_WIN}"
echo "    stop:   Ctrl+C"
echo
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec "${PY}" "${ROOT}/app.py"
