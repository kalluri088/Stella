#!/usr/bin/env bash
#
# Install Stella from a GitHub release.
#
#   curl -fsSL https://raw.githubusercontent.com/kalluri088/Stella/HEAD/install.sh | sh
#
# (HEAD, not a branch name: this stays correct whichever branch is the default.)
#
# What it does: picks a release, downloads its wheel, verifies the published
# checksum, and installs it with uv as a tool — no clone, no build, no sudo,
# nothing written outside ~/.local and uv's own tool directory.
#
# What it deliberately does NOT do: install system packages, pull multi-gigabyte
# models, edit your window-manager config, or start anything. A wheel carries
# Python and nothing else, so the recorder, transcriber, speech worker, model
# files and browser it still needs are reported by `stella doctor` at the end
# and left for you to decide about.
#
# Options (as environment variables, since `curl | sh` has no argument list):
#   STELLA_VERSION=1.5.0   install one exact version instead of the latest
#   STELLA_WHEEL=/path.whl install a wheel you already have, skipping the
#                          download (a checksum beside it is still verified)
#   STELLA_EMBED=1         also install the 'embed' extra (pulls torch, ~2 GB)
#   STELLA_MINIMAL=1       base package only: no wake, barge-in or web extras
#   STELLA_CHECK=1         resolve, download and verify, then change nothing
set -euo pipefail

REPO="kalluri088/Stella"
DEFAULT_VERSION="1.5.0"
WHEEL_BASE="stella"

say() { printf '%s\n' "$*"; }
die() { printf 'stella install: %s\n' "$*" >&2; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "this installer needs '$1' on PATH"
}

# --- the release to install ------------------------------------------------

# The tag behind /releases/latest, read from the redirect rather than the API:
# one request, no rate limit, and a failure here falls back to the version
# baked into this file instead of installing nothing.
latest_version() {
  curl -fsSI -o /dev/null -w '%{redirect_url}' \
    "https://github.com/$REPO/releases/latest" 2>/dev/null |
    sed -n 's|.*/tag/v\([0-9][0-9.]*\)$|\1|p'
}

local_wheel="${STELLA_WHEEL:-}"
if [ -n "$local_wheel" ]; then
  [ -f "$local_wheel" ] || die "no such wheel: $local_wheel"
  wheel="$(basename "$local_wheel")"
  # The version is the middle of a wheel's own name, so a local install needs
  # no metadata read and no argument.
  version="${STELLA_VERSION:-$(printf '%s' "$wheel" |
    sed -n 's/^stella-\([0-9][0-9.]*\)-py3-none-any\.whl$/\1/p')}"
else
  version="${STELLA_VERSION:-}"
  if [ -z "$version" ]; then
    need curl
    version="$(latest_version || true)"
    [ -n "$version" ] || version="$DEFAULT_VERSION"
  fi
  wheel="${WHEEL_BASE}-${version}-py3-none-any.whl"
fi
case "$version" in
  "" | *[!0-9.]*) die "could not work out a version from $wheel" ;;
esac
say "Stella $version"

# --- uv, the only thing this script may install ----------------------------

if ! command -v uv >/dev/null 2>&1; then
  [ -z "${STELLA_CHECK:-}" ] ||
    die "uv is not installed; install it first (https://docs.astral.sh/uv/)"
  need curl
  say "installing uv into ~/.local (no sudo)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv was installed but is not on PATH"
fi

# --- fetch and verify ------------------------------------------------------

verify_checksum() {
  # The published .sha256 beside the wheel is what makes "downloaded the file
  # GitHub says it is" a fact rather than a hope, so an unverifiable download
  # stops here instead of installing a page that 404'd.
  local actual expected
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$1" | cut -d' ' -f1)"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$1" | cut -d' ' -f1)"
  else
    die "no sha256 tool (sha256sum or shasum) — refusing to install unverified"
  fi
  expected="$(cut -d' ' -f1 < "$2" | tr 'A-F' 'a-f')"
  [ "$actual" = "$expected" ] ||
    die "the wheel does not match its published checksum — not installing it"
  say "checksum verified"
}

work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

if [ -n "$local_wheel" ]; then
  cp "$local_wheel" "$work/$wheel"
  if [ -f "$local_wheel.sha256" ]; then
    cp "$local_wheel.sha256" "$work/$wheel.sha256"
    verify_checksum "$work/$wheel" "$work/$wheel.sha256"
  else
    say "no checksum beside $local_wheel — installing the file as given"
  fi
else
  need curl
  base_url="https://github.com/$REPO/releases/download/v$version"
  # -f, so a 404 (a private repo, a deleted release, a wrong version) is an
  # error here rather than a saved HTML page that fails the checksum later.
  # https only, because the release URL redirects and a downgrade mid-chain
  # should not be able to land on plain http.
  curl -fL --proto '=https' --tlsv1.2 -o "$work/$wheel" \
    "$base_url/$wheel" || die "could not download $base_url/$wheel — is the release published and visible?"
  curl -fL --proto '=https' --tlsv1.2 -o "$work/$wheel.sha256" \
    "$base_url/$wheel.sha256" || die "could not download the checksum for $version — this release predates checksums"
  verify_checksum "$work/$wheel" "$work/$wheel.sha256"
fi

# --- what to install -------------------------------------------------------

# The three extras that cost a few tens of MB and make voice and web work by
# default; 'embed' is opt-in because it drags torch with it.
extras=""
if [ -n "${STELLA_MINIMAL:-}" ]; then
  say "minimal: base package only"
elif [ -n "${STELLA_EMBED:-}" ]; then
  extras="[wake,barge-in,web,embed]"
  say "with the embed extra — this pulls torch, roughly 2 GB"
else
  extras="[wake,barge-in,web]"
fi
target="$WHEEL_BASE$extras"

if [ -n "${STELLA_CHECK:-}" ]; then
  say "STELLA_CHECK is set: verified and ready to install '$target' — nothing changed"
  exit 0
fi

# --reinstall so re-running this command upgrades in place instead of
# reporting that the tool is already installed.
uv tool install --reinstall --from "$work/$wheel" "$target" ||
  die "uv could not install $target"

# --- what happens next -----------------------------------------------------

installed="$HOME/.local/bin/stella"
on_path="$(command -v stella || true)"
if [ -n "$on_path" ] && [ "$on_path" != "$installed" ]; then
  say ""
  say "note: 'stella' on your PATH resolves to $on_path, not the copy just"
  say "installed at $installed. A pip or old-venv install is shadowing it; put"
  say "~/.local/bin earlier on your PATH, or remove the other copy."
fi

say ""
say "Configuring Stella is a one-time thing: run 'stella-ui' once to pick a"
say "model (or export STELLA_MODEL), then this tells you what else this"
say "machine still needs:"
say ""
say "  stella doctor"
say ""
say "Voice needs a recorder, a transcriber and a speech worker outside the"
say "package; the wake word needs its model files; the model itself needs to"
say "be pulled. doctor names each one and where it looked."
say ""
say "For the desktop shortcut, add to ~/.config/hypr/bindings.lua — and"
say "reload, or restart the compositor:"
say ""
say '  o.bind("SUPER + D", "Stella voice", "'"$installed"' voice --toggle")'
