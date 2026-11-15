#!/bin/sh
# Install ad-arta as a LaunchDaemon: starts at boot, no login needed.
# Run with: sudo sh deploy/install-daemon.sh
# The daemon runs as the user who runs this, with ad-arta in ~/ad-arta.
set -e
[ "$(id -u)" = 0 ] && [ -n "$SUDO_USER" ] || { echo "Run with sudo from your own account"; exit 1; }
cd "$(dirname "$0")/.."
DAEMON=/Library/LaunchDaemons/com.user.ad-arta.plist

launchctl bootout system/com.user.ad-arta 2>/dev/null || true
sed "s/YOUR_USER/$SUDO_USER/g" deploy/com.user.ad-arta.plist > "$DAEMON"
chown root:wheel "$DAEMON"
chmod 644 "$DAEMON"
launchctl bootstrap system "$DAEMON"
sleep 3
launchctl print system/com.user.ad-arta | grep -E "state =|pid =" || true
echo "Done. ad-arta now starts at boot."
