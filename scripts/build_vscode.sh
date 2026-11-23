#!/usr/bin/env bash
# Builds vscode-extension/ai-meter-<version>.vsix.  Publish:  npx @vscode/vsce publish -p <token>
set -euo pipefail
cd "$(dirname "$0")/../vscode-extension"; npx -y @vscode/vsce package
