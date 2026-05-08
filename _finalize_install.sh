#!/bin/bash
set -e
cd /home/user/Gator
mv -f requirements.txt.new requirements.txt
chmod +x installer.sh bootstrap.py tools/gator_ctl.py
python3 -m py_compile bootstrap.py tools/gator_ctl.py && echo "PYOK"
bash -n installer.sh && echo "SHOK"
