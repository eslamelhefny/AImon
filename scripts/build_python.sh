#!/usr/bin/env bash
# Builds the PyPI files (dist/*.whl and dist/*.tar.gz) and checks them.
# Upload:  python -m twine upload dist/ai_meter-*   (username __token__, password = your PyPI token)
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install -q build twine
rm -f dist/ai_meter-*; python3 -m build; python3 -m twine check dist/ai_meter-*
