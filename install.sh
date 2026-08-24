#!/usr/bin/env bash
# BOSS — installer.
#
#   One line, no clone needed:
#     curl -fsSL https://raw.githubusercontent.com/ali-abassi/boss/main/install.sh | bash
#   Or from a checkout:
#     ./install.sh
#
# What it does: puts the repo at ~/boss (when run via curl), builds a small
# private Python venv for the bundled runner, links `pi-boss` into ~/.local/bin,
# registers the skill for Pi / Claude Code / Codex, and tells you the one next step.
set -euo pipefail

REPO_URL="https://github.com/ali-abassi/boss"
target="${BOSS_DIR:-$HOME/boss}"
bindir="${BOSS_BIN:-$HOME/.local/bin}"
python_bin="${BOSS_PYTHON:-python3}"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

say ""
say "  ◆  BOSS — install"
say ""

# ---------------------------------------------------------------- prerequisites
command -v git >/dev/null 2>&1 || die "git is required. macOS: xcode-select --install · Linux: apt install git"
ok "git $(git --version | awk '{print $3}')"

# Pick the first Python ≥ 3.10 we can find (Apple's /usr/bin/python3 is often 3.9).
found=""
for cand in "$python_bin" python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    found="$cand"; break
  fi
done
[ -n "$found" ] || die "Python 3.10+ is required. macOS: brew install python · Linux: apt install python3 python3-venv"
python_bin="$found"
ok "python $("$python_bin" -c 'import sys; print("%d.%d" % sys.version_info[:2])') ($(command -v "$python_bin"))"

# ---------------------------------------------------------------- source
if [ -f "$(dirname "${BASH_SOURCE[0]:-$0}")/bin/pi-boss" ] 2>/dev/null; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
else
  # Piped through bash: fetch the repo.
  if [ -d "$target/.git" ]; then
    remote="$(git -C "$target" remote get-url origin 2>/dev/null || true)"
    case "$remote" in
      "$REPO_URL"|"$REPO_URL.git"|"git@github.com:ali-abassi/boss.git") ;;
      *) die "$target already exists but is not the BOSS repository; set BOSS_DIR to another path" ;;
    esac
    warn "using the existing $target checkout unchanged; update it explicitly with git when you choose"
  else
    git clone -q "$REPO_URL" "$target"
  fi
  here="$target"
fi
ok "source $here"

# ---------------------------------------------------------------- runner venv
if [ ! -x "$here/.venv/bin/python" ]; then
  "$python_bin" -m venv "$here/.venv" 2>/dev/null || die "could not create a venv. Linux: apt install python3-venv"
fi
"$here/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$here/vendor/pi-graph/requirements.txt"
"$here/vendor/pi-graph/bin/piw" schema --json >/dev/null 2>&1 || die "the bundled runner failed to start (see $here/.venv)"
ok "bundled runner ready"

# ---------------------------------------------------------------- links
mkdir -p "$bindir"
ln -sf "$here/bin/bossctl" "$bindir/bossctl"
ln -sf "$here/bin/pi-boss" "$bindir/pi-boss"
ln -sf "$here/bin/pi-boss-quit" "$bindir/pi-boss-quit"
for d in "$HOME/.pi/agent/skills" "$HOME/.claude/skills" "$HOME/.agents/skills"; do
  mkdir -p "$d" && ln -sfn "$here" "$d/boss"
done
ok "pi-boss + pi-boss-quit → $bindir"

# ---------------------------------------------------------------- pi + codex
if command -v pi >/dev/null 2>&1; then
  ok "pi $(pi --version 2>/dev/null | head -1)"
  if [ "$(PI_CODING_AGENT_DIR="$HOME/.pi/agent" pi auth check --provider openai-codex 2>/dev/null)" = "ready" ]; then
    ok "Codex login found in your Pi — the BOSS will reuse it"
  else
    warn "no Codex login in Pi yet — the first run of pi-boss will walk you through /login"
  fi
else
  warn "Pi is not installed. Install it, then run pi-boss:"
  say "        npm install -g @earendil-works/pi-coding-agent"
fi

# ---------------------------------------------------------------- PATH
case ":$PATH:" in
  *":$bindir:"*) ;;
  *)
    rc="$HOME/.zshrc"; [ -n "${BASH_VERSION:-}" ] && [ "$(basename "${SHELL:-}")" = "bash" ] && rc="$HOME/.bashrc"
    line="export PATH=\"$bindir:\$PATH\""
    if ! grep -qsF "$line" "$rc" 2>/dev/null; then printf '\n# BOSS\n%s\n' "$line" >> "$rc"; fi
    warn "added $bindir to PATH in $rc — open a new terminal (or: $line)"
    ;;
esac

say ""
say "  next:  pi-boss"
say "         first run connects to your Codex subscription, then just talk:  \"add ~/code/my-repo\""
say ""
