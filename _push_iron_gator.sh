#!/bin/bash
set -e
cd /home/user/Gator
PAT="$1"
git push "https://Mexor-dev:${PAT}@github.com/Mexor-dev/Gator.git" main 2>&1 | sed "s|${PAT}|***|g"
echo "=== POST-PUSH ==="
git log --oneline origin/main -3 2>/dev/null || git log --oneline -3
