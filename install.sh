#!/bin/sh
# Fetch the upstream Upscaler release (binaries + models) into vendor/.
#
#   ./install.sh
#
# The release is ~222 MB: the Real-ESRGAN binary plus the AI models.
# The GUI works without it, but there is nothing to upscale with.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
URL="https://github.com/hollowaykeanho/Upscaler/releases/download/0.7.0/upscaler-v0.7.0.zip"
ZIP="$HERE/vendor/upscaler-v0.7.0.zip"

mkdir -p "$HERE/vendor"

if [ -x "$HERE/vendor/upscaler/bin/mac-amd64" ]; then
    echo "already installed: $HERE/vendor/upscaler"
    exit 0
fi

if [ ! -f "$ZIP" ]; then
    echo "downloading the release (~222 MB)..."
    curl -fL --progress-bar -o "$ZIP" "$URL"
fi

echo "unpacking..."
cd "$HERE/vendor"
unzip -q -o "$ZIP"

BIN="$HERE/vendor/upscaler/bin/mac-amd64"
chmod +x "$BIN"

echo
echo "installed. Checking that it runs:"
"$BIN" -h 2>&1 | head -3
