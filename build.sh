#!/bin/bash
# Build dist/Create.app (version taken from the VERSION file).
set -e
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Build scratch files stay out of the (synced) project folder.
WORK="${TMPDIR:-/tmp}/create-build"

echo "==> Running PyInstaller..."
uv run --no-project --python 3.12 --with-requirements requirements.txt --with pyinstaller -- \
    pyinstaller create.spec --noconfirm --clean --workpath "$WORK" --distpath dist

echo "==> Done: dist/Create.app (v$(cat VERSION))"
du -sh dist/Create.app
