#!/usr/bin/env bash
# Build, publish a GitHub release, and so ship an update to every installed copy.
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

VERSION="${1:?"Usage: ./release.sh <version> [release notes]"}"
NOTES="${2:-"Create v${VERSION}"}"
GITHUB_REPO="swildgen/create"

echo "=== Releasing Create v${VERSION} ==="

# 1. Build with the version stamp. The build and the running app both read VERSION.
echo "Step 1/4: Building..."
echo "$VERSION" > VERSION
./build.sh

# 2. Zip the app. ditto keeps the bundle's symlinks as symlinks; plain `zip -r`
#    follows them and stores every framework twice.
echo "Step 2/4: Packaging..."
ZIP="dist/Create-${VERSION}.zip"
rm -f "$ZIP"
ditto -c -k --keepParent dist/Create.app "$ZIP"
echo "  Created $ZIP ($(du -sh "$ZIP" | cut -f1), sha256 $(shasum -a 256 "$ZIP" | cut -c1-12)…)"

# 3. Commit and push
echo "Step 3/4: Committing and pushing..."
git add -A
git commit -m "Release v${VERSION}" || echo "  (nothing to commit)"
git push

# 4. GitHub Release with the zip attached. Installed apps read the latest release
#    (version from the tag, checksum from the asset) and update themselves.
echo "Step 4/4: Creating GitHub Release..."
gh release create "v${VERSION}" "$ZIP" --repo "$GITHUB_REPO" --title "Create v${VERSION}" --notes "$NOTES"

echo "=== Released Create v${VERSION} ==="
echo "  https://github.com/${GITHUB_REPO}/releases/tag/v${VERSION}"
echo "Installed copies pick it up on their next launch (or within a few hours if left open)."
