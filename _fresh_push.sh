#!/usr/bin/env bash
set -euo pipefail

cd /home/user/Gator

echo "Creating fresh orphan branch..."
git checkout --orphan fresh-build

echo "Adding all tracked files..."
git add -A

echo "Creating initial commit..."
git commit -m "Iron-Gator v1.0 - Sovereign Build

Self-contained AI substrate running 35B logic donor through 1.5B chassis.
- Native tool invocation with intent detection  
- Logic Alignment Controller with real-time steering
- 15 native tools (file, shell, memory, web, browser)
- Command Center UI on port 8000
- Telegram bot integration ready
- Zero external dependencies, 100% local
- RTX 3050 optimized (2.2GB VRAM target)

Quick start: ./bootstrap.sh && ./wakeup"

echo "Deleting old main branch..."
git branch -D main || true

echo "Renaming fresh-build to main..."
git branch -m main

echo "Force pushing to GitHub (fresh slate)..."
git push -f origin main

echo "✅ Fresh build pushed to GitHub!"
echo "Repo: https://github.com/Mexor-dev/Gator"
