#!/bin/bash
cd /home/lesego_phiri/side-projects/freqtrade
source .venv/bin/activate
echo "Starting bridge server on port 8080..."
echo "Press Ctrl+C to stop trading and shut down."
uvicorn user_data.mt5_bridge.web_server:app --host 0.0.0.0 --port 8080 --log-level warning
echo "Bridge stopped."
