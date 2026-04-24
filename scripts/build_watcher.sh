#!/usr/bin/env bash
# Build omnigraph-watcher for multiple platforms.
# Note: Tree-sitter requires CGO, so cross-compilation needs a C toolchain.

set -euo pipefail

VERSION="${VERSION:-0.1.0}"
OUT_DIR="${OUT_DIR:-./dist}"

mkdir -p "$OUT_DIR"

cd watcher

echo "=== Building omnigraph-watcher v$VERSION ==="

# macOS ARM (current dev machine)
echo "[build] darwin/arm64 ..."
GOOS=darwin GOARCH=arm64 CGO_ENABLED=1 go build \
    -ldflags="-s -w -X main.version=$VERSION" \
    -o "$OUT_DIR/omnigraph-watcher-darwin-arm64" \
    main.go

# macOS Intel
echo "[build] darwin/amd64 ..."
GOOS=darwin GOARCH=amd64 CGO_ENABLED=1 go build \
    -ldflags="-s -w -X main.version=$VERSION" \
    -o "$OUT_DIR/omnigraph-watcher-darwin-amd64" \
    main.go

# Linux AMD64 — requires cross-compiler (e.g. musl-cross or zig)
# echo "[build] linux/amd64 ..."
# CC=x86_64-linux-musl-gcc GOOS=linux GOARCH=amd64 CGO_ENABLED=1 go build \
#     -ldflags="-s -w -X main.version=$VERSION" \
#     -o "$OUT_DIR/omnigraph-watcher-linux-amd64" \
#     main.go

# Windows AMD64 — requires mingw-w64
# echo "[build] windows/amd64 ..."
# CC=x86_64-w64-mingw32-gcc GOOS=windows GOARCH=amd64 CGO_ENABLED=1 go build \
#     -ldflags="-s -w -X main.version=$VERSION" \
#     -o "$OUT_DIR/omnigraph-watcher-windows-amd64.exe" \
#     main.go

echo "=== Done. Binaries in $OUT_DIR ==="
ls -la "$OUT_DIR"
