#!/usr/bin/env bash
# Builds dist/AI-Meter-Windows.zip on Linux: bundled Windows Python + deps + AI Meter.exe launcher.
# Needs: mingw-w64 (apt install mingw-w64), curl, zip, and the wheel from build_python.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; OUT="$ROOT/dist"; W="$(mktemp -d)"; APP="$W/AI Meter"
PY_URL="${PY_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12+20251014-x86_64-pc-windows-msvc-install_only_stripped.tar.gz}"
WHEEL="$(ls -t "$OUT"/ai_meter-*.whl | head -1)"
mkdir -p "$APP"; curl -sSL "$PY_URL" | tar xz -C "$W"; mv "$W/python" "$APP/python"
rm -rf "$APP/python/"{include,libs} "$APP/python/Lib/"{test,idlelib,ensurepip,lib2to3,turtledemo}
python3 -m pip install -q --no-compile --target "$APP/python/Lib/site-packages" --platform win_amd64 --python-version 3.12 \
  --implementation cp --only-binary=:all: requests PyYAML
python3 -m pip install -q --no-compile --target "$APP/python/Lib/site-packages" --no-deps "$WHEEL"
rm -rf "$APP/python/Lib/site-packages/bin"
# Nothing from the build machine may ship: compiled files embed the build folder (and the builder's user
# name), and pip's direct_url.json records the local path of the wheel.
find "$APP/python" -name __pycache__ -prune -exec rm -rf {} +
rm -f "$APP"/python/Lib/site-packages/*.dist-info/direct_url.json
# AI Meter never installs packages at run time; the bundled pip only adds code with known CVEs.
rm -rf "$APP"/python/Lib/site-packages/pip "$APP"/python/Lib/site-packages/pip-*.dist-info "$APP"/python/Scripts/pip*
cp "$ROOT/windows/README.txt" "$APP/"
cd "$ROOT/windows"
x86_64-w64-mingw32-windres launcher.rc -O coff -o "$W/launcher.res"
x86_64-w64-mingw32-gcc -O2 -municode -mwindows -static -s launcher.c "$W/launcher.res" -o "$APP/AI Meter.exe"
rm -f "$OUT/AI-Meter-Windows.zip"; (cd "$W" && zip -q -9 -r "$OUT/AI-Meter-Windows.zip" "AI Meter")
rm -rf "$W"; echo "built $OUT/AI-Meter-Windows.zip"
