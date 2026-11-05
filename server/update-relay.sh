#!/bin/bash
# Updates only the KNX relay container (knx-relay). Other containers on the server are not touched.
set -e
DIR=/root/knx-relay
curl -fsSL "https://raw.githubusercontent.com/alialqaisi878-hash/saqar-akd-KNX-app-/main/server/knx_relay.py?t=$(date +%s)" -o "$DIR/knx_relay.py.new"
python3 -c "import ast,sys;ast.parse(open('$DIR/knx_relay.py.new').read())" 2>/dev/null || grep -q "def main" "$DIR/knx_relay.py.new"
cp "$DIR/knx_relay.py" "$DIR/knx_relay.py.old" 2>/dev/null || true
mv "$DIR/knx_relay.py.new" "$DIR/knx_relay.py"
docker restart knx-relay >/dev/null
for i in $(seq 1 30); do curl -s http://127.0.0.1:47810/knx/ | grep -q "KNX relay OK" && break; sleep 2; done
echo "Relay updated."
echo "Company dashboard: https://knx.saqar-akd.com.iq/admin/"
echo -n "Password: "; cat "$DIR/admin_password.txt"; echo
