#!/usr/bin/env bash
# One command to go from a fresh clone to a running readcast.
#
#   ./setup.sh
#
# It installs dependencies, writes a config.yml with a freshly generated
# token, creates the data directory and feed, starts both background services,
# and records the sample voices. Every step is idempotent: run it again after
# a `git pull` and it will only do what is missing.
#
# Flags:
#   --base-url URL   the URL your phone will use (default: this machine's LAN IP)
#   --no-mlx         skip the local speech engine (mlx-audio, mlx-whisper)
#   --no-services    do not install or start the launchd agents
#   --no-voices      do not sample reference voices (skips a large model download)
#   --force-config   overwrite an existing config.yml with a new token

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

BASE_URL=""
WITH_MLX=1
WITH_SERVICES=1
WITH_VOICES=1
FORCE_CONFIG=0

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url)     BASE_URL="${2:?--base-url needs a value}"; shift 2 ;;
    --no-mlx)       WITH_MLX=0; shift ;;
    --no-services)  WITH_SERVICES=0; shift ;;
    --no-voices)    WITH_VOICES=0; shift ;;
    --force-config) FORCE_CONFIG=1; shift ;;
    -h|--help)      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1 (try --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
step "Checking prerequisites"
missing=""
for tool in uv ffmpeg ffprobe; do
  command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -n "$missing" ]; then
  echo "missing:$missing" >&2
  case "$missing" in
    *uv*)     echo "  uv:     curl -LsSf https://astral.sh/uv/install.sh | sh" >&2 ;;
  esac
  case "$missing" in
    *ffmpeg*|*ffprobe*) echo "  ffmpeg: brew install ffmpeg" >&2 ;;
  esac
  die "install the tools above, then re-run ./setup.sh"
fi
info "uv, ffmpeg, ffprobe: present"

if [ "$WITH_MLX" = 1 ] && [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" != "arm64" ]; then
  info "not Apple silicon — the local speech engine will not work; continuing with --no-mlx"
  WITH_MLX=0
fi

# ------------------------------------------------------------ dependencies
step "Installing dependencies"
if [ "$WITH_MLX" = 1 ]; then
  info "including the mlx extra (speech + verification); this is the slow part"
  uv sync --extra mlx
else
  uv sync
fi
uv pip install -e . --quiet
info "virtualenv ready at .venv"

# ----------------------------------------------------------------- config
step "Configuring"
if [ -f config.yml ] && [ "$FORCE_CONFIG" = 0 ]; then
  info "config.yml already exists — leaving it alone (--force-config to replace)"
else
  [ -f config.example.yml ] || die "config.example.yml is missing from this checkout"
  if [ -z "$BASE_URL" ]; then
    lan_ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
    BASE_URL="http://${lan_ip:-127.0.0.1}:8788"
  fi
  token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cp config.example.yml config.yml
  python3 - "$BASE_URL" "$token" <<'PY'
import pathlib, sys
base_url, token = sys.argv[1], sys.argv[2]
p = pathlib.Path("config.yml")
out = []
for line in p.read_text().splitlines(keepends=True):
    if line.startswith("base_url:"):
        line = f"base_url: {base_url}\n"
    elif line.startswith("api_token:"):
        line = f"api_token: {token}\n"
    out.append(line)
p.write_text("".join(out))
PY
  info "wrote config.yml with a new api_token"
  info "base_url: $BASE_URL"
  [ "${BASE_URL#http://127.0.0.1}" != "$BASE_URL" ] &&
    info "note: 127.0.0.1 only works on this machine. Re-run with --base-url once you have a Tailscale name."
fi

step "Creating the data directory and feed"
uv run readcast init

step "Checking the pronunciation rules"
uv run readcast rules test

# --------------------------------------------------------------- services
if [ "$WITH_SERVICES" = 1 ]; then
  step "Installing and starting the background services"
  info "two launchd agents: the API on :8788 and the speech server on :8770"
  ./deploy/install.sh

  printf '    waiting for the speech server on :8770 '
  up=0
  for _ in $(seq 1 60); do
    if nc -z 127.0.0.1 8770 2>/dev/null; then up=1; break; fi
    printf '.'; sleep 1
  done
  [ "$up" = 1 ] && printf ' up\n' || printf ' not yet (check data/mlx-audio.log)\n'

  printf '    waiting for the API on :8788 '
  up=0
  for _ in $(seq 1 30); do
    if nc -z 127.0.0.1 8788 2>/dev/null; then up=1; break; fi
    printf '.'; sleep 1
  done
  [ "$up" = 1 ] && printf ' up\n' || printf ' not yet (check data/api.log)\n'
else
  info "skipping services (--no-services). Start them later with ./deploy/install.sh"
fi

# ----------------------------------------------------------------- voices
if [ "$WITH_VOICES" = 1 ] && [ "$WITH_SERVICES" = 1 ]; then
  step "Recording sample voices"
  info "the first synthesis downloads the model — several gigabytes, several minutes"
  if uv run readcast voices init; then
    info "sample clips are in data/voices/"
  else
    info "voice sampling did not finish. It is not required to submit articles;"
    info "retry later with: uv run readcast voices init"
  fi
else
  info "skipping voice sampling. Run it later with: uv run readcast voices init"
fi

# ------------------------------------------------------------------- done
step "Ready"
uv run readcast init 2>/dev/null | sed 's/^/    /'
cat <<'EOF'

    Next:
      - Subscribe your podcast app to the feed URL above. It is the credential:
        it embeds a token, so do not share it.
      - Load the browser extension: chrome://extensions -> Developer mode ->
        "Load unpacked" -> the extension/ directory. Then run
        `uv run readcast extension-link` for a one-paste setup URL that fills
        in the base_url and token for you.

    Useful:
      uv run readcast ui              dashboard URL, token filled in
      uv run readcast watch           live view of the worker
      uv run readcast add <url>       queue an article
      uv run readcast jobs            what has been submitted
      tail -f data/api.log            server log
EOF
