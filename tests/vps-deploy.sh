#!/usr/bin/env bash
# Isolated Linux rehearsal. Run from the checkout with Docker Compose, curl,
# Python 3 and the project's Playwright Chromium installed.
set -Eeuo pipefail
root=$(mktemp -d)
export KRILL_PROJECT="krilltest$(date +%s)$$"
id=$KRILL_PROJECT
edge="$id-edge"
cleanup(){
  if [[ -n "${phone_pid:-}" ]]; then kill "$phone_pid" 2>/dev/null || true; fi
  docker rm -fv "$id-proxy" >/dev/null 2>&1 || true
  for c in $(docker ps -aq --filter "label=com.docker.compose.project=$id"); do docker rm -f "$c" >/dev/null; done
  for v in "${id}_quiz_data" "${id}_quiz_backups"; do docker volume rm "$v" >/dev/null 2>&1 || true; done
  docker network rm "$edge" >/dev/null 2>&1 || true
  docker image rm "$id:a" "$id:b" "$id:base" >/dev/null 2>&1 || true
  rm -rf -- "$root"
}
trap cleanup EXIT
docker network create "$edge" >/dev/null
docker run -d --name "$id-proxy" --network "$edge" -p 127.0.0.1::443 caddy:2-alpine >/dev/null
port=$(docker port "$id-proxy" 443/tcp | sed 's/.*://')
origin="https://localhost:$port"
cat > "$root/Caddyfile" <<'CADDY'
existing.localhost {
  tls internal
  respond "Existing site"
}
CADDY
load_proxy(){
  docker cp "$root/Caddyfile" "$id-proxy:/etc/caddy/Caddyfile"
  docker exec "$id-proxy" caddy validate --config /etc/caddy/Caddyfile >/dev/null
  docker exec "$id-proxy" caddy reload --config /etc/caddy/Caddyfile
}
load_proxy
docker cp "$id-proxy:/data/caddy/pki/authorities/local/root.crt" "$root/ca.pem"
export CURL_CA_BUNDLE="$root/ca.pem"
other(){ [[ "$(curl -fsS --retry 5 --retry-all-errors --resolve "existing.localhost:$port:127.0.0.1" "https://existing.localhost:$port/")" == "Existing site" ]]; }
other
cat >> "$root/Caddyfile" <<'CADDY'
localhost {
  tls internal
  reverse_proxy krillion-quiz:3000
}
CADDY
load_proxy
mkdir -p "$root/releases" "$root/build"
printf 'DOMAIN=localhost:%s\nHOST_PASSWORD=rehearsal\nCADDY_NETWORK=%s\n' "$port" "$edge" > "$root/.env"
docker build -q -t "$id:base" . >/dev/null
for label in a b; do
  revision=$(printf "%0.s$label" {1..40})
  dir="$root/releases/$label"; mkdir "$dir"
  cp compose.vps.yaml scripts/deploy-vps.sh "$dir/"
  printf 'KRILL_IMAGE=%s:%s\nKRILL_REVISION=%s\n' "$id" "$label" "$revision" > "$dir/release.env"
  cp public/app.js "$root/build/app.js"
  printf '\nglobalThis.krillReleaseProbe="%s";\n' "${label^^}" >> "$root/build/app.js"
  python3 - "$root/build/saved.wav" "$label" <<'PY'
from pathlib import Path
import sys
sound=bytearray(Path('public/sounds/saved.wav').read_bytes())
sound[-1]=ord(sys.argv[2])
Path(sys.argv[1]).write_bytes(sound)
PY
  printf 'FROM %s:base\nENV APP_REVISION=%s\nCOPY app.js /app/public/app.js\nCOPY saved.wav /app/public/sounds/saved.wav\n' "$id" "$revision" > "$root/build/Dockerfile"
  docker build -q -t "$id:$label" "$root/build" >/dev/null
done
# Retry a first installation that stopped after creating its volume.
docker volume create "${id}_quiz_data" >/dev/null
bash "$root/releases/a/deploy-vps.sh" deploy
coproc PHONE { "${NODE:-node}" tests/vps-browser.mjs "$origin"; }
phone_pid=$PHONE_PID
exec {from_phone}<&"${PHONE[0]}" {to_phone}>&"${PHONE[1]}"
read_phone(){ local msg; read -r -t 90 msg <&"$from_phone"; [[ "${msg%$'\r'}" == "$1" ]]; }
read_phone READY
bash "$root/releases/b/deploy-vps.sh" deploy
other
echo UPGRADED >&"$to_phone"; read_phone VERIFIED
# The container is healthy, but the external endpoint serves the wrong revision.
mkdir "$root/releases/bad"
cp compose.vps.yaml scripts/deploy-vps.sh "$root/releases/bad/"
sed "s/^KRILL_REVISION=.*/KRILL_REVISION=$(printf 'c%.0s' {1..40})/" \
  "$root/releases/a/release.env" > "$root/releases/bad/release.env"
if bash "$root/releases/bad/deploy-vps.sh" deploy; then echo "Bad revision accepted" >&2; exit 1; fi
[[ "$(realpath "$root/current")" == "$root/releases/b" ]]
[[ "$(curl -fsS "$origin/healthz")" == *"$(printf 'b%.0s' {1..40})"* ]]
bash "$root/previous/deploy-vps.sh" rollback
other
echo ROLLED_BACK >&"$to_phone"; read_phone DONE
wait "$phone_pid"; unset phone_pid
echo "Passed shared HTTPS, two releases, warm browser cache, scores, failed deployment and rollback."
