#!/bin/sh
# Use the installed application with this project's own Harness and Electron state.
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
app='/Applications/DeepSeek Harness.app'
app_executable="$app/Contents/MacOS/DeepSeek Harness"
if [ ! -x "$app_executable" ]; then
  echo 'DeepSeek Harness is not installed at /Applications/DeepSeek Harness.app' >&2
  exit 1
fi
case "${1-}" in
  '') set -- ;;
  --debug) set -- --remote-debugging-address=127.0.0.1 --remote-debugging-port=19222 ;;
  *) echo 'Usage: tools/start_dsh_isolated.sh [--debug]' >&2; exit 2 ;;
esac
electron_data=/private/tmp/campus-radar-dsh-electron
if [ -L "$electron_data" ]; then
  echo 'Refusing a symlinked Electron test directory.' >&2
  exit 1
fi
if [ ! -e "$electron_data" ]; then
  mkdir -m 700 "$electron_data"
fi
if [ ! -d "$electron_data" ] || [ "$(stat -f '%u' "$electron_data")" != "$(id -u)" ]; then
  echo 'Electron test directory must be a directory owned by the current user.' >&2
  exit 1
fi
mkdir -p "$repo_root/outputs/dsh-home"
chmod 700 "$repo_root/outputs/dsh-home" "$electron_data"
cd "$repo_root"
DSH_HOME="$repo_root/outputs/dsh-home" exec "$app_executable" \
  --user-data-dir="$electron_data" "$@"
