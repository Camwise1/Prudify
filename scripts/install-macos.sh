#!/usr/bin/env bash
# Install Prudify natively on an Apple Silicon Mac, transcribing on the GPU.
#
# Docker on macOS runs a Linux VM with no access to the Apple GPU, so the
# container is CPU-only there. A native install with the mlx-whisper engine
# is several times faster on the same machine.
#
# Run from a checkout:   ./scripts/install-macos.sh
# Re-run it to upgrade after `git pull`. Settings you changed are kept.
#
# Environment overrides:
#   PRUDIFY_MODEL   Whisper model for a fresh install (default large-v3-turbo)
#   PRUDIFY_PORT    Port to serve on (default 8317)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$HOME/Library/Application Support/Prudify"
VENV="$DATA_DIR/venv"
LABEL="com.camwise.prudify"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
MODEL="${PRUDIFY_MODEL:-large-v3-turbo}"
PORT="${PRUDIFY_PORT:-8317}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "this installer is for macOS."
[[ "$(uname -m)" == "arm64" ]] || die "mlx-whisper needs an Apple Silicon (M-series) Mac."
command -v brew >/dev/null || die "Homebrew is required. Install it from https://brew.sh, then re-run."

say "Installing ffmpeg, Python and Node with Homebrew"
brew install ffmpeg python@3.12 node

BREW_PREFIX="$(brew --prefix)"
PYTHON="$BREW_PREFIX/opt/python@3.12/bin/python3.12"

say "Building the web UI"
# The committed bundle can lag the source (it is rebuilt at release time), so
# build it here rather than ship a UI without the newest settings.
(cd "$REPO/frontend" && npm install --no-audit --no-fund --silent && npm run build --silent)

say "Installing Prudify into $VENV"
mkdir -p "$DATA_DIR/logs"
[[ -x "$VENV/bin/python" ]] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --upgrade "$REPO[whisper,mlx]"
# The build rewrote the committed bundle under new hashed names; put the
# checkout back so the next `git pull` does not trip over it.
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$REPO" checkout --quiet -- backend/prudify/static
  git -C "$REPO" clean -fdq -- backend/prudify/static
fi

FRESH=0
[[ -f "$DATA_DIR/config.yaml" ]] || FRESH=1
if [[ $FRESH == 1 ]]; then
  say "Configuring mlx-whisper with model $MODEL"
  PRUDIFY_DATA_DIR="$DATA_DIR" "$VENV/bin/python" - "$MODEL" "$PORT" <<'EOF'
import sys
from prudify.config import load_config, save_config

config = load_config()
config.transcription.engine = "mlx-whisper"
config.transcription.model = sys.argv[1]
config.server.port = int(sys.argv[2])
save_config(config)
EOF
else
  echo "Existing config kept: $DATA_DIR/config.yaml"
fi

say "Downloading the Whisper model (first run only)"
# Fetch now so the first book does not stall on a silent download.
"$VENV/bin/python" - "$MODEL" <<'EOF'
import sys
from huggingface_hub import snapshot_download
from prudify.core.transcribe import mlx_model_repo

print(snapshot_download(repo_id=mlx_model_repo(sys.argv[1])))
EOF

say "Installing the login service ($PLIST)"
mkdir -p "$HOME/Library/LaunchAgents"
# caffeinate -i keeps the Mac from idle-sleeping for as long as Prudify runs;
# a sleeping Mac stalls the queue and drops its network share mounts.
# PRUDIFY_POLLING_WATCHER: SMB shares deliver no filesystem events on macOS.
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>$VENV/bin/prudify</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$BREW_PREFIX/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PRUDIFY_DATA_DIR</key><string>$DATA_DIR</string>
    <key>PRUDIFY_POLLING_WATCHER</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ExitTimeOut</key><integer>60</integer>
  <key>StandardOutPath</key><string>$DATA_DIR/logs/service.log</string>
  <key>StandardErrorPath</key><string>$DATA_DIR/logs/service.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

say "Waiting for Prudify to start"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/ping" >/dev/null 2>&1; then
    HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
    printf '\nPrudify is running:\n  http://localhost:%s\n  http://%s.local:%s  (from other machines)\n' \
      "$PORT" "$HOST" "$PORT"
    exit 0
  fi
  sleep 1
done
die "Prudify did not answer on port $PORT. See $DATA_DIR/logs/service.log"
