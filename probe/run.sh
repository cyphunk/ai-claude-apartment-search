#!/usr/bin/env bash
source ../venv/bin/activate
#python -m pip install -r ../requirements.txt
#python -m playwright install chromium
export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
python probe/application_probe.py --headful
