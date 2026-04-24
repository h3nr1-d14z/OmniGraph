#!/usr/bin/env bash
# OmniGraph Cloudflare Tunnel setup script
# Requires: cloudflared installed (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)

set -euo pipefail

TUNNEL_NAME="${TUNNEL_NAME:-omnigraph-hub}"
CONFIG_DIR="${CONFIG_DIR:-./.cloudflared}"

echo "=== OmniGraph Cloudflare Tunnel Setup ==="
echo ""

if ! command -v cloudflared &> /dev/null; then
    echo "Error: cloudflared CLI not found."
    echo "Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    exit 1
fi

if ! cloudflared tunnel list &> /dev/null; then
    echo "Please login first: cloudflared tunnel login"
    exit 1
fi

mkdir -p "$CONFIG_DIR"

# Create tunnel if not exists
if ! cloudflared tunnel info "$TUNNEL_NAME" &> /dev/null; then
    echo "Creating tunnel: $TUNNEL_NAME"
    cloudflared tunnel create "$TUNNEL_NAME"
else
    echo "Tunnel $TUNNEL_NAME already exists"
fi

TUNNEL_ID=$(cloudflared tunnel list | grep "$TUNNEL_NAME" | awk '{print $1}')
echo "Tunnel ID: $TUNNEL_ID"

# Write config
cat > "$CONFIG_DIR/config.yml" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CONFIG_DIR/$TUNNEL_ID.json

ingress:
  - hostname: $TUNNEL_NAME.trycloudflare.com
    service: http://localhost:8000
  - service: http_status:404
EOF

echo ""
echo "=== Config written to $CONFIG_DIR/config.yml ==="
echo ""
echo "To start the tunnel:"
echo "  cloudflared tunnel run $TUNNEL_NAME"
echo ""
echo "Or run as a service:"
echo "  cloudflared service install"
echo "  cloudflared tunnel route dns $TUNNEL_NAME $TUNNEL_NAME.trycloudflare.com"
