#!/bin/bash
cd /home/user/Gator
lsof -ti:8000 | xargs kill -9 2>/dev/null
sleep 1
nohup venv/bin/python src/interfaces/command_center.py --host 127.0.0.1 --port 8000 > logs/ui.log 2>&1 &
echo "UI server restarted with PID $!"
sleep 2
curl -s http://127.0.0.1:8000/api/health | head -1
