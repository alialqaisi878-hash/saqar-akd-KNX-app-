#!/bin/bash
# Installs the KNX relay as its own Docker container behind the existing Traefik (n8n-traefik-1).
# It does not stop or change any other container. Path: https://HOST/knx/...
set -e
HOST="${1:-$(hostname).hstgr.cloud}"
APPHOST="${2:-}"   # optional company subdomain, e.g. app.company.com (whole subdomain goes to the relay)
DIR=/root/knx-relay
TRAEFIK=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /traefik/ {print $1; exit}')
if [ -z "$TRAEFIK" ]; then echo "Traefik container not found"; exit 1; fi
ARGS=$(docker inspect "$TRAEFIK" --format '{{join .Config.Cmd " "}} {{join .Args " "}}')
RESOLVER=$(echo "$ARGS" | grep -o -- '--certificatesresolvers\.[A-Za-z0-9_-]*\.' | head -1 | cut -d. -f2)
EP443=$(echo "$ARGS" | grep -o -- '--entrypoints\.[A-Za-z0-9_-]*\.address=:443' | head -1 | cut -d. -f2)
NET=$(docker inspect "$TRAEFIK" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
EP443=${EP443:-websecure}
echo "Traefik: $TRAEFIK  network: $NET  entrypoint: $EP443  certresolver: ${RESOLVER:-none}  host: $HOST"

TLS_LABELS=(--label "traefik.http.routers.knxrelay.tls=true")
[ -n "$RESOLVER" ] && TLS_LABELS+=(--label "traefik.http.routers.knxrelay.tls.certresolver=$RESOLVER")
if [ -n "$APPHOST" ]; then
  TLS_LABELS+=(--label "traefik.http.routers.knxapp.rule=Host(\`$APPHOST\`)"
               --label "traefik.http.routers.knxapp.entrypoints=$EP443"
               --label "traefik.http.routers.knxapp.tls=true"
               --label "traefik.http.routers.knxapp.service=knxrelay")
  [ -n "$RESOLVER" ] && TLS_LABELS+=(--label "traefik.http.routers.knxapp.tls.certresolver=$RESOLVER")
  TLS_LABELS+=(--label "traefik.http.routers.knxrelay.service=knxrelay")
fi

docker rm -f knx-relay >/dev/null 2>&1 || true
docker run -d --name knx-relay --restart unless-stopped \
  --network "$NET" \
  -p 127.0.0.1:47810:47810 \
  -v "$DIR":/app -e KNX_RELAY_DATA=/app -e KNX_RELAY_PORT=47810 -e PYTHONUNBUFFERED=1 \
  --label "traefik.enable=true" \
  --label "traefik.docker.network=$NET" \
  --label "traefik.http.routers.knxrelay.rule=Host(\`$HOST\`) && PathPrefix(\`/knx\`)" \
  --label "traefik.http.routers.knxrelay.priority=1000" \
  --label "traefik.http.routers.knxrelay.entrypoints=$EP443" \
  "${TLS_LABELS[@]}" \
  --label "traefik.http.services.knxrelay.loadbalancer.server.port=47810" \
  python:3.12-slim sh -c "pip install -q --no-cache-dir aiohttp==3.14.5 && python /app/knx_relay.py"

echo "Waiting for it to start..."
for i in $(seq 1 40); do
  if curl -s http://127.0.0.1:47810/knx/ | grep -q "KNX relay OK"; then break; fi; sleep 2
done
curl -s http://127.0.0.1:47810/knx/ && echo "  (local OK)"
sleep 5
echo "Public check:"; curl -s -m 20 "https://$HOST/knx/" || true; echo
if [ -n "$APPHOST" ]; then
  echo "Company domain check (certificate can take up to a minute):"
  for i in 1 2 3 4 5 6; do curl -s -m 15 "https://$APPHOST/" | grep -q "KNX relay OK" && { echo "https://$APPHOST/  OK"; break; }; sleep 10; done
fi
echo
echo "=============================================="
echo " Relay URL for config.json:  https://$HOST/knx"
echo "=============================================="
