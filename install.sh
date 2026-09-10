#!/usr/bin/env bash
# =============================================================================
# orchestratord — One-Click Install & Deploy Script
# =============================================================================
#
# Usage:
#   ./install.sh                          # interactive mode
#   ./install.sh --backends clawcodex,codex   # specific backends
#   ./install.sh --no-backends            # core only
#   ./install.sh --all-backends           # install all backends
#   ./install.sh --help                   # show help
#
# Environment variables:
#   ORCHESTRATORD_REPO     repo URL or local path (default: auto-detect)
#   ORCHESTRATORD_BRANCH   git branch to checkout (default: main)
#   ORCHESTRATORD_INSTALL_PREFIX  install prefix for venv (default: ~/.orchestratord)
#   CLAWCODEX_SOURCE       path to clawcodex-ascend source (for clawcodex backend)
#   NO_COLOR               set to 1 to disable colored output
# =============================================================================

set -euo pipefail

# ── Color helpers ────────────────────────────────────────────────────────────
if [[ -z "${NO_COLOR:-}" ]] && [[ -t 1 ]]; then
    BOLD='\033[1m'
    RED='\033[31m'
    GREEN='\033[32m'
    YELLOW='\033[33m'
    BLUE='\033[34m'
    CYAN='\033[36m'
    DIM='\033[2m'
    RESET='\033[0m'
else
    BOLD='' RED='' GREEN='' YELLOW='' BLUE='' CYAN='' DIM='' RESET=''
fi

# ── Constants ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_PREFIX="${HOME}/.orchestratord"
MIN_PYTHON_VERSION="3.11"

# Backend metadata
# Each backend has: display_name, runtime_type, check_function, install_hint
declare -A BACKEND_DISPLAY_NAME
declare -A BACKEND_RUNTIME_TYPE
declare -A BACKEND_CHECK_CMD
declare -A BACKEND_INSTALL_HINT

BACKEND_DISPLAY_NAME[clawcodex]="ClawCodex (InProcess SDK)"
BACKEND_RUNTIME_TYPE[clawcodex]="source"
BACKEND_CHECK_CMD[clawcodex]='_check_clawcodex'
BACKEND_INSTALL_HINT[clawcodex]="Install clawcodex from https://gitcode.com/chadwweng/clawcodex, or set CLAWCODEX_SOURCE=/path/to/clawcodex (auto-detected from common locations if unset)"

BACKEND_DISPLAY_NAME[claude]="Anthropic Claude Code (CLI)"
BACKEND_RUNTIME_TYPE[claude]="binary"
BACKEND_CHECK_CMD[claude]='_check_binary claude ccb'
BACKEND_INSTALL_HINT[claude]="Install Claude Code: npm install -g @anthropic-ai/claude-code, or install the open-source fork ccb"

BACKEND_DISPLAY_NAME[codex]="OpenAI Codex (CLI)"
BACKEND_RUNTIME_TYPE[codex]="binary"
BACKEND_CHECK_CMD[codex]='_check_binary codex'
BACKEND_INSTALL_HINT[codex]="Install OpenAI Codex CLI: npm install -g @openai/codex"

BACKEND_DISPLAY_NAME[dsh]="DeepSeek Harness (SDK)"
BACKEND_RUNTIME_TYPE[dsh]="package"
BACKEND_CHECK_CMD[dsh]='_check_dsh'
BACKEND_INSTALL_HINT[dsh]="pip install deepseek-harness-sdk && ensure 'dsh' binary is on PATH"

BACKEND_DISPLAY_NAME[hermes]="Hermes (CLI)"
BACKEND_RUNTIME_TYPE[hermes]="binary"
BACKEND_CHECK_CMD[hermes]='_check_binary hermes'
BACKEND_INSTALL_HINT[hermes]="Install Hermes CLI from its upstream repository"

BACKEND_DISPLAY_NAME[opencode]="OpenCode (Protocol)"
BACKEND_RUNTIME_TYPE[opencode]="binary"
BACKEND_CHECK_CMD[opencode]='_check_binary opencode'
BACKEND_INSTALL_HINT[opencode]="Install OpenCode: npm install -g opencode"

BACKEND_DISPLAY_NAME[cursor]="Cursor (Cli)"
BACKEND_RUNTIME_TYPE[cursor]="binary"
BACKEND_CHECK_CMD[cursor]='_check_binary cursor-agent'
BACKEND_INSTALL_HINT[cursor]="Install Cursor CLI (cursor-agent): see https://cursor.com/cli — event stream shape is not yet exercised in-tree, output is buffered as a single TEXT event"

BACKEND_DISPLAY_NAME[copilot]="GitHub Copilot (Cli)"
BACKEND_RUNTIME_TYPE[copilot]="binary"
BACKEND_CHECK_CMD[copilot]='_check_binary copilot'
BACKEND_INSTALL_HINT[copilot]="Install GitHub Copilot CLI: gh extension install github/gh-copilot — event stream needs experimentation (FEATURE_GAP §8.1)"

BACKEND_DISPLAY_NAME[kimi]="Kimi (Cli)"
BACKEND_RUNTIME_TYPE[kimi]="binary"
BACKEND_CHECK_CMD[kimi]='_check_binary kimi'
BACKEND_INSTALL_HINT[kimi]="Install Kimi CLI (Moonshot AI): see https://platform.moonshot.cn — Chinese-language prompts are first-class"

BACKEND_DISPLAY_NAME[qwen]="Qwen / DashScope (Cli/stream-json)"
BACKEND_RUNTIME_TYPE[qwen]="binary"
BACKEND_CHECK_CMD[qwen]='_check_binary qwen'
BACKEND_INSTALL_HINT[qwen]="Install Qwen CLI (DashScope) with --output-format stream-json support: see https://help.aliyun.com/zh/dashscope — only P1 backend that genuinely claims streaming_deltas=True"

BACKEND_DISPLAY_NAME[kiro-cli]="AWS Kiro (Cli)"
BACKEND_RUNTIME_TYPE[kiro-cli]="binary"
BACKEND_CHECK_CMD[kiro-cli]='_check_binary kiro'
BACKEND_INSTALL_HINT[kiro-cli]="Install AWS Kiro CLI (kiro binary) — event stream shape not yet exercised, output buffered as a single TEXT event"

BACKEND_DISPLAY_NAME[openclaw]="OpenClaw (Cli)"
BACKEND_RUNTIME_TYPE[openclaw]="binary"
BACKEND_CHECK_CMD[openclaw]='_check_binary openclaw'
BACKEND_INSTALL_HINT[openclaw]="Install OpenClaw CLI (openclaw binary) — §8.1 HTTP/Gateway path deferred, output buffered as TEXT"

BACKEND_DISPLAY_NAME[reasonix]="Reasonix (Cli)"
BACKEND_RUNTIME_TYPE[reasonix]="binary"
BACKEND_CHECK_CMD[reasonix]='_check_binary reasonix'
BACKEND_INSTALL_HINT[reasonix]="Install Reasonix CLI (reasonix binary) — event stream shape not yet exercised"

BACKEND_DISPLAY_NAME[zeroclaw]="ZeroClaw (Cli)"
BACKEND_RUNTIME_TYPE[zeroclaw]="binary"
BACKEND_CHECK_CMD[zeroclaw]='_check_binary zeroclaw'
BACKEND_INSTALL_HINT[zeroclaw]="Install ZeroClaw CLI (zeroclaw binary) — event stream shape not yet exercised"

BACKEND_DISPLAY_NAME[acp]="ACP (Agent Client Protocol)"
BACKEND_RUNTIME_TYPE[acp]="binary"
BACKEND_CHECK_CMD[acp]='_check_binary grok codebuddy qwenpaw qodercli qoderclicn deveco'
BACKEND_INSTALL_HINT[acp]="Install an ACP vendor runtime (grok, codebuddy, qwenpaw, qodercli, qoderclicn, or deveco) — generic stdio ACP backend"

ALL_BACKENDS="clawcodex claude codex dsh hermes opencode cursor copilot kimi qwen kiro-cli openclaw reasonix zeroclaw acp"

# ── Help ─────────────────────────────────────────────────────────────────────
show_help() {
    cat <<EOF
${BOLD}orchestratord — One-Click Install & Deploy${RESET}

${BOLD}Usage:${RESET}
  ./install.sh [OPTIONS]

${BOLD}Options:${RESET}
  --backends NAME,...     Install specific backends (comma-separated)
  --no-backends           Install core only, no backends
  --all-backends          Install all available backends
  --prefix PATH           Install prefix (default: ~/.orchestratord)
  --repo URL              Git repository URL for orchestratord
  --branch NAME           Git branch to checkout (default: main)
  --python PATH           Python interpreter to use (default: auto-detect)
  --no-venv               Install into current Python environment (no venv)
  --with-db               Initialize/migrate the database schema after install
  --with-web              Build the Next.js Web client (apps/web)
                          (requires Node.js >= 20.9 + pnpm >= 9; runs
                          pnpm install at the repo root to resolve the
                          monorepo workspace:* deps)
  --reset                 (with --with-web) wipe the project's pnpm state
                          (node_modules, .pnpm store) before installing.
                          Use to recover from a broken install.
  --dry-run               Show what would be done, don't execute
  --help                  Show this help message

${BOLD}Available Backends:${RESET}
EOF
    for b in $ALL_BACKENDS; do
        printf "  ${CYAN}%-14s${RESET} %s\n" "$b" "${BACKEND_DISPLAY_NAME[$b]}"
    done
    echo
    echo "${BOLD}Examples:${RESET}"
    echo "  ./install.sh                              # interactive, asks about each backend"
    echo "  ./install.sh --backends clawcodex,codex   # specific backends"
    echo "  ./install.sh --no-backends                # core only"
    echo "  ./install.sh --all-backends --dry-run     # preview all"
    echo
    echo "${BOLD}Environment:${RESET}"
    echo "  CLAWCODEX_SOURCE    path to clawcodex-ascend for the clawcodex backend"
    echo "  NO_COLOR=1          disable colored output"
}

# ── Utility functions ────────────────────────────────────────────────────────
info()    { echo -e "  ${BLUE}→${RESET} $*"; }
success() { echo -e "  ${GREEN}✓${RESET} $*"; }
warn()    { echo -e "  ${YELLOW}⚠${RESET} $*"; }
error()   { echo -e "  ${RED}✗${RESET} $*"; }
header()  { echo -e "\n${BOLD}${CYAN}═══ $* ═══${RESET}\n"; }
step()    { echo -e "${BOLD}── $* ──${RESET}"; }

_check_binary() {
    for bin in "$@"; do
        if command -v "$bin" &>/dev/null; then
            echo "$bin"
            return 0
        fi
    done
    return 1
}

_check_python_pkg() {
    "${PYTHON}" -c "import $1" 2>/dev/null
}

_check_dsh() {
    # DeepSeek Harness needs both the Python SDK and the dsh binary
    if ! _check_python_pkg deepseek_harness_sdk; then
        return 1
    fi
    if ! _check_binary dsh >/dev/null 2>&1; then
        return 1
    fi
    echo "dsh"
    return 0
}

_check_clawcodex() {
    # Auto-detect clawcodex source location. Priority:
    #   1. CLAWCODEX_SOURCE env var (explicit override)
    #   2. Common paths in $HOME
    #   3. Standard system paths
    local candidate
    local candidates=()

    # 1. Explicit env var
    if [[ -n "${CLAWCODEX_SOURCE:-}" ]]; then
        candidates+=("$CLAWCODEX_SOURCE")
    fi

    # 2. Home directory paths
    candidates+=(
        "${HOME}/clawcodex"
        "${HOME}/clawcodex-ascend"
    )

    # 3. Standard system paths
    candidates+=("/opt/clawcodex")

    for candidate in "${candidates[@]}"; do
        if [[ -d "$candidate" ]] && [[ -f "$candidate/extensions/api/query.py" ]]; then
            if PYTHONPATH="$candidate:${PYTHONPATH:-}" "${PYTHON}" -c "from extensions.api.query import QueryRunner" 2>/dev/null; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

_pip_install() {
    if [[ "$USE_VENV" == "true" ]]; then
        "${VENV_PYTHON}" -m pip install "$@"
    else
        "${PYTHON}" -m pip install "$@"
    fi
}

_pip_install_editable() {
    if [[ "$USE_VENV" == "true" ]]; then
        "${VENV_PYTHON}" -m pip install -e "$@"
    else
        "${PYTHON}" -m pip install -e "$@"
    fi
}

# ── Workspace / version helpers ─────────────────────────────────────────────
# Required for --with-web (Next.js 16 monorepo build). Optional otherwise.
MIN_NODE_VERSION="20.9.0"
MIN_PNPM_VERSION="9.0.0"

_version_ge() {
    # Returns 0 if $1 >= $2 (dot-separated numeric versions).
    local cur="$1" min="$2"
    local IFS='.'
    local -a cur_parts=($cur) min_parts=($min)
    local i
    for i in 0 1 2; do
        local c=${cur_parts[$i]:-0}
        local m=${min_parts[$i]:-0}
        if (( c > m )); then return 0; fi
        if (( c < m )); then return 1; fi
    done
    return 0
}

_assert_workspace_layout() {
    # The web client lives in a pnpm workspace; workspace:* deps resolve
    # at the monorepo root (where pnpm-workspace.yaml lives) BEFORE
    # `next build` is invoked. A sparse / partial checkout that is missing
    # packages/* fails with a cryptic link error from pnpm; surface the
    # real cause up front.
    local src="$1"
    local missing=()

    if [[ ! -f "${src}/pnpm-workspace.yaml" ]]; then
        missing+=("pnpm-workspace.yaml (repo root)")
    fi
    if [[ ! -f "${src}/apps/web/package.json" ]]; then
        missing+=("apps/web/package.json")
    fi

    # Runtime workspace packages required by apps/web (workspace:* deps).
    local required_pkgs=(
        "packages/app-contracts/package.json"
        "packages/app-issue-pr/package.json"
        "packages/core/package.json"
        "packages/ui/package.json"
        "packages/views/package.json"
    )
    local rel
    for rel in "${required_pkgs[@]}"; do
        if [[ ! -f "${src}/${rel}" ]]; then
            missing+=("${rel}")
        fi
    done

    if (( ${#missing[@]} > 0 )); then
        error "Monorepo layout incomplete — the web build needs:"
        local m
        for m in "${missing[@]}"; do
            echo "    - ${m}"
        done
        echo ""
        echo "  Ensure you cloned the full repository (not a sparse / partial checkout)."
        return 1
    fi
}

_verify_web_integrity() {
    # After pnpm install reports success, confirm a key workspace package is
    # actually resolvable. pnpm can claim "Done" while leaving broken symlinks
    # (e.g. apps/web/node_modules/next → .pnpm/.../next when .pnpm/ wasn't
    # populated from a global store). Catching this before `next build` saves
    # a 30-second compile → immediate MODULE_NOT_FOUND failure cycle.
    local web_dir="$1"
    local next_bin="${web_dir}/node_modules/next/dist/bin/next"
    if [[ -e "$next_bin" ]]; then
        return 0
    fi
    error "Web workspace integrity check FAILED"
    echo "  Expected: ${next_bin}"
    if [[ -L "$next_bin" ]]; then
        local target
        target=$(readlink "$next_bin")
        echo "  Found:    broken symlink → ${target}"
    else
        echo "  Found:    missing"
    fi
    echo ""
    echo "  pnpm install reported success, but a key workspace package is not"
    echo "  resolvable. This typically means the local pnpm virtual store"
    echo "  (node_modules/.pnpm) was not populated."
    echo ""
    echo "  Recovery:"
    echo "    1. Re-run with --reset: ./install.sh --with-web --reset"
    echo "    2. Or manually:"
    echo "         rm -rf node_modules apps/*/node_modules packages/*/node_modules"
    echo "         pnpm install"
    return 1
}

_reset_node_modules() {
    # Wipe the project's pnpm state so the next install starts from a clean
    # virtual store. Only the web (Node) side is affected — Python venv /
    # orchestratord pip install are untouched.
    local src="$1"
    info "Resetting pnpm state at ${src} (deleting node_modules / .pnpm)…"
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  [DRY RUN] rm -rf ${src}/node_modules"
        echo "  [DRY RUN] find ${src}/apps ${src}/packages -type d -name node_modules -prune -exec rm -rf {} +"
        echo "  [DRY RUN] rm -rf ${src}/node_modules/.pnpm"
        return 0
    fi
    rm -rf "${src}/node_modules"
    # Each workspace member keeps its own node_modules; wipe them all.
    local sub
    for sub in apps packages; do
        if [[ -d "${src}/${sub}" ]]; then
            find "${src}/${sub}" -type d -name node_modules -prune -exec rm -rf {} + 2>/dev/null || true
        fi
    done
    rm -rf "${src}/node_modules/.pnpm"
    success "pnpm state reset"
}

_format_duration() {
    # Render a non-negative integer of seconds as a short human string.
    local s=${1:-0}
    if (( s < 60 )); then
        echo "${s}s"
    elif (( s < 3600 )); then
        echo "$(( s / 60 ))m$(( s % 60 ))s"
    else
        echo "$(( s / 3600 ))h$(( (s % 3600) / 60 ))m"
    fi
}

# ── Prerequisite checks ──────────────────────────────────────────────────────
check_prerequisites() {
    header "Checking Prerequisites"

    PYTHON="${USER_PYTHON:-$(command -v python3 || command -v python || echo '')}"
    if [[ -z "$PYTHON" ]]; then
        error "Python >= ${MIN_PYTHON_VERSION} is required but not found"
        exit 1
    fi
    local py_ver
    py_ver=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    if ! "$PYTHON" -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)"; then
        error "Python ${MIN_PYTHON_VERSION}+ is required, found ${py_ver}"
        exit 1
    fi
    success "Python ${py_ver} — ${PYTHON}"

    if ! command -v git &>/dev/null; then
        error "git is required but not found"
        exit 1
    fi
    success "git — $(git --version | head -1)"

    if ! "$PYTHON" -m pip --version &>/dev/null; then
        error "pip is not available for ${PYTHON}"
        exit 1
    fi
    success "pip — $("$PYTHON" -m pip --version)"

    if command -v uv &>/dev/null; then
        HAS_UV=true
        success "uv — $(uv --version 2>/dev/null || echo 'installed')"
    else
        HAS_UV=false
        info "uv not found (optional); will use pip for installation"
    fi

    # Node.js + pnpm are only required when --with-web is requested, but
    # probe them here so the user gets one consolidated, actionable error
    # path. Skip the hard checks when the web client is not in scope.
    check_node_and_pnpm
}

check_node_and_pnpm() {
    local node_ok=false
    local pnpm_ok=false
    local node_ver=""
    local pnpm_ver=""

    if command -v node &>/dev/null; then
        node_ver=$(node --version 2>/dev/null | sed 's/^v//' | head -c 32)
        if _version_ge "$node_ver" "$MIN_NODE_VERSION"; then
            node_ok=true
            success "node — v${node_ver}"
        else
            warn "node v${node_ver} found, but >= ${MIN_NODE_VERSION} required for --with-web"
        fi
    else
        info "node not found (required for --with-web; install Node.js ${MIN_NODE_VERSION}+)"
    fi

    if command -v pnpm &>/dev/null; then
        pnpm_ver=$(pnpm --version 2>/dev/null | head -c 32)
        if _version_ge "$pnpm_ver" "$MIN_PNPM_VERSION"; then
            pnpm_ok=true
            success "pnpm — v${pnpm_ver}"
        else
            warn "pnpm v${pnpm_ver} found, but >= ${MIN_PNPM_VERSION} required for --with-web"
        fi
    else
        info "pnpm not found (required for --with-web; see install hints below)"
    fi

    if [[ "$WITH_WEB" == "true" ]]; then
        if [[ "$node_ok" != "true" ]]; then
            error "Node.js ${MIN_NODE_VERSION}+ is required for --with-web (Next.js 16)"
            echo "    Install: https://nodejs.org/  (or use nvm: nvm install 20)"
            exit 1
        fi
        if [[ "$pnpm_ok" != "true" ]]; then
            error "pnpm ${MIN_PNPM_VERSION}+ is required for --with-web"
            echo "    Install via Corepack (ships with Node.js):"
            echo "        corepack enable && corepack prepare pnpm@latest --activate"
            echo "    Or:    npm install -g pnpm"
            echo "    See:   https://pnpm.io/installation"
            exit 1
        fi
    fi
}

# ── Venv setup ───────────────────────────────────────────────────────────────
setup_venv() {
    if [[ "$USE_VENV" != "true" ]]; then
        return 0
    fi

    header "Setting Up Virtual Environment"

    INSTALL_PREFIX="${USER_PREFIX:-$DEFAULT_INSTALL_PREFIX}"
    VENV_DIR="${INSTALL_PREFIX}/venv"

    if [[ "$DRY_RUN" == "true" ]]; then
        info "Would create virtual environment at ${VENV_DIR}"
        VENV_PYTHON="${PYTHON}"
        return 0
    fi

    if [[ -d "$VENV_DIR" ]]; then
        info "Virtual environment exists at ${VENV_DIR}"
        read -r -p "  Recreate? [y/N] " recreate
        if [[ "$recreate" =~ ^[Yy]$ ]]; then
            rm -rf "$VENV_DIR"
            "$PYTHON" -m venv "$VENV_DIR"
            success "Recreated virtual environment"
        fi
    else
        info "Creating virtual environment at ${VENV_DIR}"
        mkdir -p "$INSTALL_PREFIX"
        "$PYTHON" -m venv "$VENV_DIR"
        success "Created virtual environment"
    fi

    # Resolve venv python path
    if [[ -f "${VENV_DIR}/bin/python" ]]; then
        VENV_PYTHON="${VENV_DIR}/bin/python"
    elif [[ -f "${VENV_DIR}/Scripts/python.exe" ]]; then
        VENV_PYTHON="${VENV_DIR}/Scripts/python.exe"
    else
        error "Cannot find python in venv at ${VENV_DIR}"
        exit 1
    fi

    cat > "${INSTALL_PREFIX}/activate.sh" <<'ACTIVATE'
#!/usr/bin/env bash
# Source this file to activate the orchestratord virtual environment
# Usage: source ~/.orchestratord/activate.sh
VENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/venv"
if [[ -f "${VENV_DIR}/bin/activate" ]]; then
    source "${VENV_DIR}/bin/activate"
elif [[ -f "${VENV_DIR}/Scripts/activate" ]]; then
    source "${VENV_DIR}/Scripts/activate"
fi
echo "orchestratord venv activated. Run 'orchestratord --help' to get started."
ACTIVATE
    chmod +x "${INSTALL_PREFIX}/activate.sh"
    success "Created activation script: ${INSTALL_PREFIX}/activate.sh"
}

# ── Core installation ────────────────────────────────────────────────────────
install_core() {
    header "Installing orchestratord Core"

    if [[ -f "${SCRIPT_DIR}/pyproject.toml" ]] && grep -q 'name = "orchestratord"' "${SCRIPT_DIR}/pyproject.toml" 2>/dev/null; then
        ORCH_SRC="${SCRIPT_DIR}"
        info "Using local source: ${ORCH_SRC}"
    elif [[ -n "${ORCHESTRATORD_REPO:-}" ]]; then
        REPO_URL="$ORCHESTRATORD_REPO"
        if [[ -d "$REPO_URL" ]]; then
            ORCH_SRC="$REPO_URL"
            info "Using local path: ${ORCH_SRC}"
        else
            ORCH_SRC="${INSTALL_PREFIX}/src"
            if [[ -d "$ORCH_SRC" ]]; then
                info "Source directory exists, would pull latest..."
                if [[ "$DRY_RUN" != "true" ]]; then
                    (cd "$ORCH_SRC" && git fetch && git checkout "${ORCHESTRATORD_BRANCH:-main}" && git pull)
                fi
            else
                info "Would clone orchestratord from ${REPO_URL}..."
                if [[ "$DRY_RUN" != "true" ]]; then
                    git clone --branch "${ORCHESTRATORD_BRANCH:-main}" "$REPO_URL" "$ORCH_SRC"
                fi
            fi
            success "Source ready at ${ORCH_SRC}"
        fi
    else
        ORCH_SRC="${SCRIPT_DIR}"
        info "Using script directory as source: ${ORCH_SRC}"
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        info "Would install orchestratord core from ${ORCH_SRC}"
        return 0
    fi

    if [[ "$HAS_UV" == "true" ]] && [[ "$USE_VENV" == "true" ]]; then
        info "Installing via uv (fast)..."
        (cd "$ORCH_SRC" && uv pip install --python "$VENV_PYTHON" -e ".[dev]")
    else
        info "Installing via pip..."
        _pip_install_editable "${ORCH_SRC}[dev]"
    fi

    local orch_bin
    if [[ "$USE_VENV" == "true" ]]; then
        orch_bin="${VENV_DIR}/bin/orchestratord"
    else
        orch_bin="$(command -v orchestratord 2>/dev/null || echo '')"
    fi
    if [[ -n "$orch_bin" ]] && "$orch_bin" --help &>/dev/null; then
        success "orchestratord CLI installed"
    else
        warn "orchestratord CLI may not be on PATH yet"
        if [[ "$USE_VENV" == "true" ]]; then
            info "Activate with: source ${INSTALL_PREFIX}/activate.sh"
        fi
    fi
}

# ── Backend installation ─────────────────────────────────────────────────────
check_runtime() {
    local backend="$1"
    local check_fn="${BACKEND_CHECK_CMD[$backend]}"
    local result
    if result=$(eval "$check_fn" 2>/dev/null); then
        echo "$result"
        return 0
    fi
    return 1
}

install_backend_package() {
    local backend="$1"
    local pkg="orchestratord-${backend}"
    local pkg_dir="${ORCH_SRC}/backends/${pkg}"

    if [[ -d "$pkg_dir" ]]; then
        info "Installing ${pkg} from local source..."
        if [[ "$DRY_RUN" != "true" ]]; then
            if [[ "$HAS_UV" == "true" ]] && [[ "$USE_VENV" == "true" ]]; then
                (cd "$ORCH_SRC" && uv pip install --python "$VENV_PYTHON" -e "$pkg_dir")
            else
                _pip_install_editable "$pkg_dir"
            fi
        fi
    else
        info "Installing ${pkg} from PyPI..."
        if [[ "$DRY_RUN" != "true" ]]; then
            _pip_install "$pkg"
        fi
    fi
    if [[ "$DRY_RUN" == "true" ]]; then
        success "Would install ${pkg}"
    else
        success "Backend package installed: ${pkg}"
    fi
}

install_backend() {
    local backend="$1"
    local display="${BACKEND_DISPLAY_NAME[$backend]}"

    step "${display} (orchestratord-${backend})"

    local runtime_path
    if runtime_path=$(check_runtime "$backend"); then
        if [[ "${BACKEND_RUNTIME_TYPE[$backend]}" == "source" ]]; then
            success "Agent runtime found at: ${runtime_path}"
        elif [[ "${BACKEND_RUNTIME_TYPE[$backend]}" == "package" ]]; then
            success "Agent runtime available"
        else
            success "Agent runtime found: ${runtime_path}"
        fi
    else
        echo ""
        warn "Agent runtime NOT found for ${display}"
        echo "  ${DIM}${BACKEND_INSTALL_HINT[$backend]}${RESET}"
        echo ""
        if [[ "$NONINTERACTIVE" == "true" ]]; then
            info "Non-interactive mode: skipping backend installation"
            return 1
        fi
        read -r -p "  Install backend package anyway? (runtime will fail until agent is installed) [y/N] " confirm
        if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
            info "Skipped ${backend}"
            return 1
        fi
    fi

    install_backend_package "$backend"
    return 0
}

select_backends() {
    header "Backend Selection"

    if [[ "$NO_BACKENDS" == "true" ]]; then
        info "Skipping all backends (--no-backends)"
        return 0
    fi

    if [[ "$ALL_BACKENDS_FLAG" == "true" ]]; then
        SELECTED_BACKENDS="$ALL_BACKENDS"
        info "Installing all backends: ${SELECTED_BACKENDS// /, }"
        return 0
    fi

    if [[ -n "${USER_BACKENDS:-}" ]]; then
        SELECTED_BACKENDS="$USER_BACKENDS"
        info "Installing specified backends: ${SELECTED_BACKENDS// /, }"
        return 0
    fi

    if [[ "$NONINTERACTIVE" == "true" ]]; then
        info "Non-interactive mode: no backends selected (use --backends to specify)"
        SELECTED_BACKENDS=""
        return 0
    fi

    echo ""
    echo "  Available backends:"
    local i=1
    for b in $ALL_BACKENDS; do
        printf "  ${CYAN}%d)${RESET} %-14s  %s\n" "$i" "$b" "${BACKEND_DISPLAY_NAME[$b]}"
        ((i++))
    done
    echo "  ${CYAN}0)${RESET}  Skip all backends (core only)"
    echo "  ${CYAN}a)${RESET}  Install all backends"
    echo ""

    read -r -p "  Select backends (comma-separated numbers, 0=skip, a=all, default=0): " selection
    selection="${selection:-0}"

    if [[ "$selection" == "0" ]]; then
        SELECTED_BACKENDS=""
        info "No backends selected"
    elif [[ "$selection" == "a" ]] || [[ "$selection" == "A" ]]; then
        SELECTED_BACKENDS="$ALL_BACKENDS"
    else
        SELECTED_BACKENDS=""
        local -a idx_arr
        IFS=',' read -ra idx_arr <<< "$selection"
        local names=($ALL_BACKENDS)
        for idx in "${idx_arr[@]}"; do
            idx=$(echo "$idx" | xargs)
            if [[ "$idx" =~ ^[0-9]+$ ]] && (( idx >= 1 && idx <= ${#names[@]} )); then
                SELECTED_BACKENDS="${SELECTED_BACKENDS} ${names[$((idx-1))]}"
            fi
        done
        SELECTED_BACKENDS="${SELECTED_BACKENDS# }"
    fi
}

install_backends() {
    INSTALLED_BACKENDS=""
    local installed=0
    local skipped=0

    for backend in $SELECTED_BACKENDS; do
        if install_backend "$backend"; then
            INSTALLED_BACKENDS="${INSTALLED_BACKENDS} ${backend}"
            ((installed++)) || true
        else
            ((skipped++)) || true
        fi
    done
    INSTALLED_BACKENDS="${INSTALLED_BACKENDS# }"

    if (( installed > 0 )); then
        echo ""
        success "Installed ${installed} backend(s): ${INSTALLED_BACKENDS// /, }"
    fi
    if (( skipped > 0 )); then
        info "Skipped ${skipped} backend(s) (agent runtime not found)"
    fi
}

# ── Verification ─────────────────────────────────────────────────────────────
verify_installation() {
    header "Verifying Installation"

    if [[ "$DRY_RUN" == "true" ]]; then
        info "Dry run — skipping verification"
        return 0
    fi

    local orch_bin
    if [[ "$USE_VENV" == "true" ]]; then
        orch_bin="${VENV_DIR}/bin/orchestratord"
    else
        orch_bin="$(command -v orchestratord 2>/dev/null || echo '')"
    fi

    if [[ -z "$orch_bin" ]]; then
        warn "Cannot find orchestratord CLI for verification"
        return 0
    fi

    if ! "$orch_bin" --help &>/dev/null; then
        warn "orchestratord CLI check failed"
        return 0
    fi
    success "orchestratord CLI works"

    echo ""
    info "Discovered backends:"
    "$orch_bin" server list-backends 2>/dev/null || "$PYTHON" -c "
from orchestratord.backend_registry import list_backends
for b in list_backends():
    print(f\"  - {b['name']:20s} ({b['family']})\")
" 2>/dev/null || warn "Could not list backends (not critical)"

    echo ""
    info "Available skills:"
    "$orch_bin" skills list 2>/dev/null || true
}

# ── Summary ──────────────────────────────────────────────────────────────────
print_summary() {
    header "Installation Complete"

    local total_duration=""
    if [[ -n "${TOTAL_START:-}" ]]; then
        total_duration="  $(_format_duration $(($(date +%s) - TOTAL_START)))"
    fi

    echo "  ${BOLD}orchestratord core${RESET}  installed$(_step_time_str "${STEP_CORE_DURATION:-}")"
    if [[ -n "${INSTALLED_BACKENDS:-}" ]]; then
        echo "  ${BOLD}Backends${RESET}          ${INSTALLED_BACKENDS// /, }$(_step_time_str "${STEP_BACKENDS_DURATION:-}")"
    else
        echo "  ${BOLD}Backends${RESET}          none (core only)"
    fi
    if [[ "$WITH_WEB" == "true" ]]; then
        if [[ "$WEB_BUILT_OK" == "true" ]]; then
            echo "  ${BOLD}Web client${RESET}        built (apps/web/.next)$(_step_time_str "${STEP_WEB_DURATION:-}")"
        else
            echo "  ${BOLD}Web client${RESET}        FAILED (see errors above; run ./install.sh --with-web again)"
        fi
    fi
    if [[ "$WITH_DB" == "true" && -n "${STEP_DB_DURATION:-}" ]]; then
        echo "  ${BOLD}DB schema${RESET}          migrated$(_step_time_str "${STEP_DB_DURATION}")"
    fi

    if [[ -n "$total_duration" ]]; then
        echo "  ${BOLD}──────────${RESET}"
        echo "  ${BOLD}Total${RESET}              ${total_duration#  }"
    fi

    if [[ "$USE_VENV" == "true" ]]; then
        echo ""
        echo "  ${BOLD}To activate:${RESET}"
        echo "    source ${INSTALL_PREFIX}/activate.sh"
        echo ""
        echo "  ${BOLD}Or add to PATH:${RESET}"
        echo "    export PATH=\"${VENV_DIR}/bin:\$PATH\""
    fi

    echo ""
    echo "  ${BOLD}Quick start:${RESET}"
    echo "    orchestratord --help"
    echo "    orchestratord server list-backends"
    echo "    orchestratord workflow init --kind local"
    echo "    orchestratord server start --workflow ./workflow.md"
    if [[ "$WITH_WEB" == "true" ]]; then
        echo ""
        echo "  ${BOLD}Web client (default :3100):${RESET}"
        echo "    orchestratord web                       # production (next start)"
        echo "    orchestratord web --dev                 # hot-reload dev server"
        echo "    orchestratord serve --with-web          # daemon + web together"
        echo "    pnpm --filter @orchestratord/web dev    # in-repo dev server"
    fi
    echo ""
}

# Helper: format a step duration as "  (Xs)" or "" if unset/zero.
_step_time_str() {
    local d=${1:-}
    if [[ -z "$d" || "$d" == "0" ]]; then
        echo ""
    else
        echo "  ($(_format_duration "$d"))"
    fi
}

# ── Argument parsing ─────────────────────────────────────────────────────────
parse_args() {
    NO_BACKENDS=false
    ALL_BACKENDS_FLAG=false
    NONINTERACTIVE=false
    DRY_RUN=false
    USE_VENV=true
    WITH_WEB=false
    WITH_DB=false
    WEB_BUILT_OK=false
    RESET_NODE_MODULES=false
    USER_BACKENDS=""
    USER_PREFIX=""
    USER_PYTHON=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help|-h)
                show_help
                exit 0
                ;;
            --no-backends)
                NO_BACKENDS=true
                NONINTERACTIVE=true
                shift
                ;;
            --all-backends)
                ALL_BACKENDS_FLAG=true
                NONINTERACTIVE=true
                shift
                ;;
            --backends)
                USER_BACKENDS="${2//,/ }"
                NONINTERACTIVE=true
                shift 2
                ;;
            --backends=*)
                USER_BACKENDS="${1#*=}"
                USER_BACKENDS="${USER_BACKENDS//,/ }"
                NONINTERACTIVE=true
                shift
                ;;
            --prefix)
                USER_PREFIX="$2"
                shift 2
                ;;
            --prefix=*)
                USER_PREFIX="${1#*=}"
                shift
                ;;
            --repo)
                ORCHESTRATORD_REPO="$2"
                shift 2
                ;;
            --repo=*)
                ORCHESTRATORD_REPO="${1#*=}"
                shift
                ;;
            --branch)
                ORCHESTRATORD_BRANCH="$2"
                shift 2
                ;;
            --branch=*)
                ORCHESTRATORD_BRANCH="${1#*=}"
                shift
                ;;
            --python)
                USER_PYTHON="$2"
                shift 2
                ;;
            --python=*)
                USER_PYTHON="${1#*=}"
                shift
                ;;
            --no-venv)
                USE_VENV=false
                shift
                ;;
            --with-web)
                WITH_WEB=true
                shift
                ;;
            --reset)
                RESET_NODE_MODULES=true
                shift
                ;;
            --with-db)
                WITH_DB=true
                shift
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            *)
                error "Unknown option: $1"
                echo "  Run with --help for usage"
                exit 1
                ;;
        esac
    done

    if [[ -n "$USER_BACKENDS" ]]; then
        for b in $USER_BACKENDS; do
            local found=false
            for known in $ALL_BACKENDS; do
                if [[ "$b" == "$known" ]]; then
                    found=true
                    break
                fi
            done
            if [[ "$found" != "true" ]]; then
                error "Unknown backend: ${b}"
                echo "  Available: ${ALL_BACKENDS// /, }"
                exit 1
            fi
        done
    fi
}

# ── Database setup (--with-db, §3.5) ────────────────────────────────────────
setup_database() {
    local orch_cli=""
    for candidate in \
        "${VENV_DIR}/bin/orchestratord" \
        "${VENV_DIR}/Scripts/orchestratord.exe"; do
        if [[ -x "$candidate" ]]; then
            orch_cli="$candidate"
            break
        fi
    done
    if [[ -z "$orch_cli" ]]; then
        orch_cli="$(command -v orchestratord || true)"
    fi
    if [[ -z "$orch_cli" ]]; then
        warn "orchestratord CLI not found — skipping database setup"
        return 1
    fi

    step "Setting up database schema"
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  [DRY RUN] ${orch_cli} db migrate  (fallback: db init)"
        return 0
    fi
    # Alembic migrations are the canonical path; a fresh checkout that has
    # not been baselined yet can fall back to create_all.
    if ! "$orch_cli" db migrate; then
        warn "db migrate failed — falling back to db init (create_all)"
        "$orch_cli" db init
    fi
}

# ── Web client build (--with-web, §3.5) ─────────────────────────────────────
build_web_client() {
    # The web client lives in a pnpm workspace; workspace:* deps MUST be
    # resolved at the monorepo root (where pnpm-workspace.yaml lives) before
    # `next build` runs. Running pnpm install from inside apps/web either
    # errors out or links incomplete transpilePackages sources.
    step "Building Next.js web client (apps/web)"

    if ! _assert_workspace_layout "${ORCH_SRC}"; then
        return 1
    fi

    if [[ "$RESET_NODE_MODULES" == "true" ]]; then
        _reset_node_modules "${ORCH_SRC}"
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  [DRY RUN] (cd ${ORCH_SRC} && pnpm install --prefer-offline --config.confirmModulesPurge=false)"
        echo "  [DRY RUN] (cd ${ORCH_SRC} && pnpm --filter @orchestratord/web build --config.confirmModulesPurge=false)"
        return 0
    fi

    info "Resolving monorepo workspace deps (pnpm install at ${ORCH_SRC})..."
    WEB_STEP_START=$(date +%s)
    # --prefer-offline: prefer the local pnpm store / cache; only hit the
    #   registry on a miss. Resilient in environments with intermittent
    #   network access (CI runners, behind proxies, etc.).
    # confirmModulesPurge=false: pnpm 10 refuses to wipe node_modules in a
    #   non-TTY environment (CI, piped scripts). Set explicitly so the
    #   install can proceed unattended.
    if (cd "$ORCH_SRC" && pnpm install --prefer-offline --config.confirmModulesPurge=false); then
        success "Workspace deps resolved ($(_format_duration $(($(date +%s) - WEB_STEP_START))))"
    elif (cd "$ORCH_SRC" && pnpm install --offline --config.confirmModulesPurge=false); then
        warn "pnpm install with --prefer-offline failed; succeeded in --offline (cache-only) mode"
        success "Workspace deps resolved ($(_format_duration $(($(date +%s) - WEB_STEP_START))), offline)"
    else
        error "pnpm install failed in both --prefer-offline and --offline modes"
        echo "  Common causes:"
        echo "    - registry unreachable AND local pnpm cache empty/missing required packages,"
        echo "    - workspace in an inconsistent state (e.g. broken symlinks from a prior failed install)."
        echo "  Recovery: re-run with --reset:"
        echo "      ./install.sh --with-web --reset"
        return 1
    fi

    if ! _verify_web_integrity "${ORCH_SRC}/apps/web"; then
        return 1
    fi

    info "Building @orchestratord/web (next build)..."
    WEB_STEP_START=$(date +%s)
    (cd "$ORCH_SRC" && pnpm --filter @orchestratord/web build --config.confirmModulesPurge=false) || {
        error "next build failed"
        return 1
    }
    success "Web client built at apps/web/.next ($(_format_duration $(($(date +%s) - WEB_STEP_START))))"
    WEB_BUILT_OK=true
}

# ── Main ─────────────────────────────────────────────────────────────────────
main() {
    parse_args "$@"
    TOTAL_START=$(date +%s)

    echo ""
    echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}${CYAN}║${RESET}     ${BOLD}orchestratord — One-Click Install & Deploy${RESET}              ${BOLD}${CYAN}║${RESET}"
    echo -e "${BOLD}${CYAN}║${RESET}     Agent-agnostic orchestration daemon                    ${BOLD}${CYAN}║${RESET}"
    echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════════╝${RESET}"
    echo ""

    if [[ "$DRY_RUN" == "true" ]]; then
        echo -e "  ${YELLOW}[DRY RUN]${RESET} No changes will be made\n"
    fi

    STEP_START=$(date +%s)
    check_prerequisites
    STEP_PREREQ_DURATION=$(($(date +%s) - STEP_START))

    STEP_START=$(date +%s)
    setup_venv
    STEP_VENV_DURATION=$(($(date +%s) - STEP_START))

    STEP_START=$(date +%s)
    install_core
    STEP_CORE_DURATION=$(($(date +%s) - STEP_START))

    select_backends
    if [[ -n "${SELECTED_BACKENDS:-}" ]]; then
        STEP_START=$(date +%s)
        install_backends
        STEP_BACKENDS_DURATION=$(($(date +%s) - STEP_START))
    fi
    if [[ "$WITH_DB" == "true" ]]; then
        STEP_START=$(date +%s)
        setup_database || true
        STEP_DB_DURATION=$(($(date +%s) - STEP_START))
    fi
    if [[ "$WITH_WEB" == "true" ]]; then
        STEP_START=$(date +%s)
        build_web_client || true
        STEP_WEB_DURATION=$(($(date +%s) - STEP_START))
    fi
    verify_installation
    print_summary
}

main "$@"