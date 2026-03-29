#!/bin/bash
# Verify gogcli is installed and working

set -e

if ! command -v gog &> /dev/null; then
    echo "❌ Error: gogcli not found in PATH"
    echo "   Binary should be at /usr/local/bin/gog"
    exit 1
fi

echo "✅ gogcli is installed: $(which gog)"
echo "   Version: $(gog --version 2>&1 || echo 'version unavailable')"
exit 0
