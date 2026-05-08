#!/usr/bin/env bash
set -euo pipefail

cd /home/user/Gator

echo "Creating fresh orphan branch without old UI..."
git checkout --orphan fresh-no-ui

echo "Adding all files..."
git add -A

echo "Creating commit..."
git commit -m "Iron-Gator v1.0 - Command Center Only

Single UI on port 8000 - old webui (port 8080) removed.

Features:
- 35B logic donor grafted to 1.5B chassis via logic_map.gate
- Native tool invocation with intent detection  
- Logic Alignment Controller with real-time steering
- Command Center UI on port 8000 (HTMX-driven)
- 15 native tools: file, shell, memory, web, browser
- Meta-tools system for tool composition
- Telegram bot integration ready
- Zero external dependencies, 100% local
- RTX 3050 optimized (2.2GB VRAM target)

Quick start: ./bootstrap.sh && ./wakeup
Then open http://127.0.0.1:8000"

echo "Deleting old main branch..."
git branch -D main 2>/dev/null || true

echo "Renaming to main..."
git branch -m main

echo "Force pushing to GitHub..."
git push -f origin main

echo ""
echo "✅ Clean build pushed to GitHub!"
echo "   - Old UI (port 8080) removed"
echo "   - Only Command Center (port 8000) remains"
echo "Repo: https://github.com/Mexor-dev/Gator"
