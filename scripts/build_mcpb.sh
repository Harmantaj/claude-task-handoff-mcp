#!/bin/bash
# Build dist/task-handoff-<version>.mcpb (Claude Desktop extension). Requires node/npx.
set -euo pipefail
cd "$(dirname "$0")/.."
version=$(python3 -c "import json; print(json.load(open('bundle/manifest.json'))['version'])")
pkg_version=$(python3 -c "import re; print(re.search(r'__version__ = \"(.+)\"', open('task_handoff/__init__.py').read()).group(1))")
[ "$version" = "$pkg_version" ] || { echo "manifest version $version != package version $pkg_version" >&2; exit 1; }

rm -rf build/bundle && mkdir -p build/bundle/server dist
cp bundle/manifest.json build/bundle/
cp bundle/server/main.py build/bundle/server/
python3 scripts/make_icon.py build/bundle/icon.png
rsync -a --exclude '__pycache__' task_handoff build/bundle/server/
cp LICENSE build/bundle/

npx -y @anthropic-ai/mcpb@2 validate build/bundle/manifest.json
npx -y @anthropic-ai/mcpb@2 pack build/bundle "dist/task-handoff-$version.mcpb"
# Stable name for the permanent link releases/latest/download/task-handoff.mcpb
cp "dist/task-handoff-$version.mcpb" dist/task-handoff.mcpb
