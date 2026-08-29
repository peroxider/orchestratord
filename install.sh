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

ALL_BACKENDS="clawcodex claude codex dsh hermes opencode"

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

    echo "  ${BOLD}orchestratord core${RESET}  installed"
    if [[ -n "${INSTALLED_BACKENDS:-}" ]]; then
        echo "  ${BOLD}Backends${RESET}          ${INSTALLED_BACKENDS// /, }"
    else
        echo "  ${BOLD}Backends${RESET}          none (core only)"
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
    echo ""
}

# ── Argument parsing ─────────────────────────────────────────────────────────
parse_args() {
    NO_BACKENDS=false
    ALL_BACKENDS_FLAG=false
    NONINTERACTIVE=false
    DRY_RUN=false
    USE_VENV=true
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

# ── Main ─────────────────────────────────────────────────────────────────────
main() {
    parse_args "$@"

    echo ""
    echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}${CYAN}║${RESET}     ${BOLD}orchestratord — One-Click Install & Deploy${RESET}              ${BOLD}${CYAN}║${RESET}"
    echo -e "${BOLD}${CYAN}║${RESET}     Agent-agnostic orchestration daemon                    ${BOLD}${CYAN}║${RESET}"
    echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════════╝${RESET}"
    echo ""

    if [[ "$DRY_RUN" == "true" ]]; then
        echo -e "  ${YELLOW}[DRY RUN]${RESET} No changes will be made\n"
    fi

    check_prerequisites
    setup_venv
    install_core
    select_backends
    if [[ -n "${SELECTED_BACKENDS:-}" ]]; then
        install_backends
    fi
    verify_installation
    print_summary
}

main "$@"