#!/usr/bin/env bash
# Install the CLI and pysci from Git into clean temporary environments, then run
# `sci --help` and the offline local workflow.
#
# Usage: cli/tests/install_check.sh [REPO_URL] [REF]
#   REPO_URL  default: $SCI_REPO_URL or https://github.com/azaho/sci-dev
#             (a local clone works as file:///path/to/clone)
#   REF       default: $SCI_REPO_REF or main
# Set SCI_INSTALL_CHECK_PIP=0 to skip the pip variant.
set -euo pipefail

REPO_URL="${1:-${SCI_REPO_URL:-https://github.com/azaho/sci-dev}}"
REF="${2:-${SCI_REPO_REF:-main}}"
PYSCI="pysci @ git+${REPO_URL}@${REF}#subdirectory=python"
CLI="sci-cli @ git+${REPO_URL}@${REF}#subdirectory=cli"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/sci-install-check.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
export UV_TOOL_DIR="$WORK/uv-tools"
export UV_TOOL_BIN_DIR="$WORK/uv-bin"
export UV_CACHE_DIR="$WORK/uv-cache"
unset VIRTUAL_ENV

workflow() {
  local sci="$1" dir="$2"
  export SCI_HOME="$dir/state"
  mkdir -p "$dir/draft/files"
  cd "$dir"
  printf '%s\n' '{"title": "Install check", "abstract": "Checks the installed CLI.", "keywords": ["install"]}' \
    > draft/sci.json
  printf '# Install check\n\nThe installed sci command works.\n' > draft/README.md
  printf 'a,b\n1,2\n' > draft/files/data.csv
  "$sci" --version
  "$sci" --help > /dev/null
  "$sci" keygen > /dev/null
  "$sci" license draft > /dev/null
  "$sci" check draft > /dev/null
  "$sci" sign draft --no-timestamp --output campaign
  local bundle
  bundle="$(ls campaign/*.zip)"
  "$sci" verify "$bundle" > /dev/null
  "$sci" search "install" --in campaign | grep -q "Install check"
  "$sci" read "$bundle" | grep -q "The installed sci command works."
  "$sci" publish campaign --to mirror > /dev/null
  test -f "mirror/$(basename "$bundle")"
  cd - > /dev/null
}

echo "== uv tool install from ${REPO_URL}@${REF}"
uv tool install --with "$PYSCI" "$CLI"
"$UV_TOOL_BIN_DIR/sci" --help | head -n 1
workflow "$UV_TOOL_BIN_DIR/sci" "$WORK/uv-run"

if [ "${SCI_INSTALL_CHECK_PIP:-1}" != "0" ]; then
  echo "== pip install from ${REPO_URL}@${REF}"
  PYTHON="${PYTHON:-python3}"
  "$PYTHON" -m venv "$WORK/venv"
  "$WORK/venv/bin/pip" install --quiet "$PYSCI" "$CLI"
  "$WORK/venv/bin/sci" --help | head -n 1
  workflow "$WORK/venv/bin/sci" "$WORK/pip-run"
fi

echo "install check passed"
