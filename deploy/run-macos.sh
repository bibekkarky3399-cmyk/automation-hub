#!/usr/bin/env bash
# Helix — clean macOS bootstrap + run app.py
#
# Usage (from repo root or this folder):
#   bash deploy/run-macos.sh
#   ./deploy/run-macos.sh
#   bash deploy/run-macos.sh --fresh     # always wipe .venv and recreate
#   bash deploy/run-macos.sh --no-run    # setup only, do not start the app
#
# Requirements:
#   - macOS
#   - Python 3.11+ (3.12 or 3.13 recommended; Homebrew: brew install python@3.13)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

VENV_DIR="${ROOT}/.venv"
VENV_PY="${VENV_DIR}/bin/python3"
FRESH=0
DO_RUN=1

for arg in "$@"; do
  case "${arg}" in
    --fresh|-f) FRESH=1 ;;
    --no-run)   DO_RUN=0 ;;
    --help|-h)
      sed -n '2,14p' "$0"
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

# --- macOS host check ---
[[ "$(uname -s)" == "Darwin" ]] || die "This script is for macOS only (uname=$(uname -s))."

venv_is_macos() {
  [[ -x "${VENV_PY}" || -x "${VENV_DIR}/bin/python" ]]
}

# True when bin/python exists but pip never finished installing (e.g. Ctrl+C mid-venv).
venv_has_pip() {
  local py=""
  if [[ -x "${VENV_PY}" ]]; then
    py="${VENV_PY}"
  elif [[ -x "${VENV_DIR}/bin/python" ]]; then
    py="${VENV_DIR}/bin/python"
  else
    return 1
  fi
  "${py}" -c "import pip" >/dev/null 2>&1
}

# Prefer versions with solid wheels for OCR/Paddle; fall back to python3.
resolve_python() {
  local cand ver
  for cand in \
    python3.13 python3.12 python3.11 \
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
    python3
  do
    if command -v "${cand}" >/dev/null 2>&1 || [[ -x "${cand}" ]]; then
      if "${cand}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1; then
        ver="$("${cand}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        echo "${cand}"
        echo "  (Python ${ver})" >&2
        return 0
      fi
    fi
  done
  return 1
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
if [[ -d "${VENV_DIR}" ]]; then
  find "${VENV_DIR}" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
fi
echo "Caches cleared."

# --- 2) Drop Windows / broken / forced venv ---
if [[ "${FRESH}" -eq 1 && -e "${VENV_DIR}" ]]; then
  log "Removing .venv (--fresh)"
  rm -rf "${VENV_DIR}"
elif [[ -e "${VENV_DIR}" ]] && ! venv_is_macos; then
  log "Existing .venv is not a macOS venv (missing bin/python3) — removing"
  if [[ -d "${VENV_DIR}/Scripts" ]]; then
    warn "Detected Windows-style .venv/Scripts — recreating for macOS."
  fi
  rm -rf "${VENV_DIR}"
elif venv_is_macos && ! venv_has_pip; then
  log "Existing .venv is incomplete (no pip) — removing and recreating"
  warn "Usually caused by interrupting venv creation (Ctrl+C during ensurepip)."
  rm -rf "${VENV_DIR}"
elif venv_is_macos; then
  log "Keeping existing macOS .venv"
fi

# --- 3) Create macOS .venv ---
HOST_PY="$(resolve_python)" || die "Python 3.11+ not found. Try:  brew install python@3.13"
log "Using interpreter: ${HOST_PY}"
"${HOST_PY}" -c "import sys; print(sys.version)"

if [[ ! -x "${VENV_PY}" && ! -x "${VENV_DIR}/bin/python" ]]; then
  log "Creating macOS venv at .venv"
  "${HOST_PY}" -m venv "${VENV_DIR}"
fi

if [[ -x "${VENV_PY}" ]]; then
  PY="${VENV_PY}"
elif [[ -x "${VENV_DIR}/bin/python" ]]; then
  PY="${VENV_DIR}/bin/python"
else
  die "venv created but no usable python under .venv/bin/"
fi

log "Venv Python: ${PY}"
"${PY}" -c "import sys; print(sys.executable); print(sys.version); assert sys.version_info >= (3, 11)"

# --- 4) Point Helix jobs at macOS venv python ---
SCRIPTS_JSON="${ROOT}/config/scripts.json"
if [[ -f "${SCRIPTS_JSON}" ]]; then
  log "Setting config/scripts.json python → .venv/bin/python3"
  "${PY}" - <<'PY'
from pathlib import Path
import json
import re

path = Path("config/scripts.json")
text = path.read_text(encoding="utf-8")
mac_py = ".venv/bin/python3"
replacement = f'"python": "{mac_py}"'
new_text, n = re.subn(r'"python"\s*:\s*"[^"]*"', replacement, text, count=1)
if n != 1:
    raise SystemExit(f"could not patch python path in {path} (matches={n})")
path.write_text(new_text, encoding="utf-8")
json.loads(path.read_text(encoding="utf-8"))
print("updated + validated", path, "→", mac_py)
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
  warn "Full requirements install failed."
  warn "Installing core Helix web deps so the app can still start…"
  "${PY}" -m pip install "flask>=3.0.0" "jinja2>=3.1.0" "jsonschema>=4.22.0" \
    || die "Could not install core dependencies."
fi

log "Playwright Chromium (PNR) — best effort"
"${PY}" -m playwright install chromium || warn "Playwright browser install skipped/failed (PNR may not work until fixed)."

# --- 7) Run ---
if [[ "${DO_RUN}" -eq 0 ]]; then
  log "Setup complete (--no-run). Start later with:"
  echo "  ${PY} app.py"
  exit 0
fi

log "Starting Helix → http://127.0.0.1:5050"
echo "    python: ${PY}"
echo "    stop:   Ctrl+C"
echo
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec "${PY}" "${ROOT}/app.py"
