#!/bin/sh
set -e
CONFIG=/data/options.json
export HA_URL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['ha_url'])")
export HA_TOKEN=$(python3 -c "import json; print(json.load(open('$CONFIG'))['ha_token'])")
export API_PORT=$(python3 -c "import json; print(json.load(open('$CONFIG'))['api_port'])")
export VACUUM_URL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['vacuum_url'])")
export SWITCHBOT_URL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['switchbot_url'])")
export SPOTIFY_URL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['spotify_url'])")
export HUE_URL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['hue_url'])")
export ANALYTICS_URL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['analytics_url'])")
export DOORBELL_URL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['doorbell_url'])")
export COOPER_SCHEDULE=$(python3 -c "import json; print(json.load(open('$CONFIG'))['cooper_schedule'])")
export POLL_INTERVAL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['poll_interval'])")
echo "[INFO] TARS Home Intelligence v1.0.0"
echo "[INFO] Port: ${API_PORT}, Poll: ${POLL_INTERVAL}s"
exec python3 /app/server.py