#!/usr/bin/env bash
# Blitz CLI Installer — macOS & Linux
# Run with: curl -fsSL https://raw.githubusercontent.com/carlelieser/blitz-cli/main/install.sh | bash

set -e

INSTALL_DIR="$HOME/.blitz-cli"
REPO_ZIP="https://github.com/carlelieser/blitz-cli/archive/refs/heads/main.zip"

step() { echo; echo ">> $*"; }
ok()   { echo "   $*"; }
warn() { echo "   $*"; }
fail() { echo "   ERROR: $*" >&2; exit 1; }

OS="$(uname -s)"

# ── Python ────────────────────────────────────────────────────────────────────

step "Checking Python"
if command -v python3 &>/dev/null; then
    ok "Python found: $(python3 --version)"
else
    warn "Python not found — installing ..."
    if [[ "$OS" == "Darwin" ]]; then
        command -v brew &>/dev/null || \
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        brew install python@3
    elif command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y python3 python3-pip
    else
        fail "Could not install Python automatically. Please install it from https://python.org."
    fi
fi

# ── Node.js ───────────────────────────────────────────────────────────────────

step "Checking Node.js"
if command -v node &>/dev/null; then
    ok "Node.js found: $(node --version)"
else
    warn "Node.js not found — installing ..."
    if [[ "$OS" == "Darwin" ]]; then
        brew install node
    elif command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y nodejs npm
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y nodejs npm
    else
        fail "Could not install Node.js automatically. Please install it from https://nodejs.org."
    fi
fi

# ── Install blitz-cli ─────────────────────────────────────────────────────────

step "Installing blitz-cli to $INSTALL_DIR"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -fsSL "$REPO_ZIP" -o "$TMP/blitz-cli.zip"
unzip -q "$TMP/blitz-cli.zip" -d "$TMP"

rm -rf "$INSTALL_DIR"
cp -r "$TMP/blitz-cli-main" "$INSTALL_DIR"

ok "Installed to $INSTALL_DIR"

# ── blitz shim ────────────────────────────────────────────────────────────────

step "Creating blitz command"

cat > "$INSTALL_DIR/blitz" <<'EOF'
#!/usr/bin/env bash
exec python3 "$HOME/.blitz-cli/blitz.py" "$@"
EOF
chmod +x "$INSTALL_DIR/blitz"

ok "Blitz shim created"

# ── PATH ──────────────────────────────────────────────────────────────────────

step "Updating PATH"

if [[ "$SHELL" == *"zsh"* ]]; then
    RC="$HOME/.zshrc"
elif [[ "$SHELL" == *"bash"* ]]; then
    RC="$HOME/.bashrc"
else
    RC=""
fi

if [[ -n "$RC" ]]; then
    if ! grep -q ".blitz-cli" "$RC" 2>/dev/null; then
        echo 'export PATH="$HOME/.blitz-cli:$PATH"' >> "$RC"
        ok "Added to $RC"
    else
        ok "Already in $RC — no changes needed"
    fi
fi

export PATH="$INSTALL_DIR:$PATH"

# ── Run ───────────────────────────────────────────────────────────────────────

step "Patching Blitz"
python3 "$INSTALL_DIR/blitz.py"

echo ""
echo "  blitz              re-download, install, and patch"
echo "  blitz patch        patch existing Blitz installation"
echo "  blitz patch <file> patch using a local installer"
echo "  blitz update       update blitz-cli itself"
echo ""
echo "Restart your terminal for 'blitz' to work in new sessions."

echo ""
