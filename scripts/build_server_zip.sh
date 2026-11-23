#!/usr/bin/env bash
# Builds dist/aimon-repo-files.zip, the bundle the VPS downloads on every start.
# The VPS expects a folder named aimon-repo/ with aimeter.py and entrypoint.sh inside.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; OUT="$ROOT/dist"; TMP="$(mktemp -d)"
mkdir -p "$OUT" "$TMP/aimon-repo"
cp "$ROOT/src/ai_meter/cli.py" "$TMP/aimon-repo/aimeter.py"
cp "$ROOT"/src/ai_meter/{dashboard.html,config.example.yaml,config.starter.yaml,mark.svg} "$TMP/aimon-repo/"
cp "$ROOT/docker/entrypoint.sh" "$ROOT/requirements.txt" "$TMP/aimon-repo/"
rm -f "$OUT/aimon-repo-files.zip"; (cd "$TMP" && zip -q -9 -r "$OUT/aimon-repo-files.zip" aimon-repo)
rm -rf "$TMP"; echo "built $OUT/aimon-repo-files.zip"
