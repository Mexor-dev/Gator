#!/usr/bin/env bash
# Remove old webui (port 8080) - Command Center (port 8000) is the new UI
set -euo pipefail

cd /home/user/Gator

echo "=== Removing Old UI Files ==="

# Stop webui if running
pkill -f "webui.py" 2>/dev/null || true

# Remove webui Python file
echo "Removing src/interfaces/webui.py"
rm -f src/interfaces/webui.py

# Remove webui CSS
echo "Removing src/interfaces/static/webui.css"
rm -f src/interfaces/static/webui.css

# Remove webui restart scripts
echo "Removing restart scripts"
rm -f restart_webui.sh _iron_restart_webui.sh

# Remove webui templates (all external template files)
echo "Removing template files"
rm -rf src/interfaces/templates/

# Remove from git if tracked
git rm -f --ignore-unmatch src/interfaces/webui.py src/interfaces/static/webui.css restart_webui.sh _iron_restart_webui.sh 2>/dev/null || true
git rm -rf --ignore-unmatch src/interfaces/templates/ 2>/dev/null || true

echo "✅ Old UI files removed"
echo ""
echo "Remaining UI:"
echo "  - Command Center: http://127.0.0.1:8000 (port 8000)"
