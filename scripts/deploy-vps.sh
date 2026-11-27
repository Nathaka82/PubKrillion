#!/usr/bin/env bash
# Runs inside /opt/krillion/releases/<release>/ on the VPS.
set -Eeuo pipefail
umask 077
mode=${1:-deploy}
case "$mode" in deploy|rollback) ;; *) echo "Usage: $0 deploy|rollback" >&2; exit 2 ;; esac
release=$(cd -- "$(dirname -- "$0")" && pwd -P)
root=$(cd "$release/../.." && pwd -P)
project=${KRILL_PROJECT:-krillion}
[[ "$release" == "$root/releases/"* && -f "$root/.env" ]] || { echo "Missing installation or private .env" >&2; exit 1; }
exec 9>"$root/deploy.lock"
flock -w 300 9
current=""
if [[ -L "$root/current" ]]; then current=$(realpath -e "$root/current"); fi

compose() {
  local dir=$1; shift
  docker compose --project-name "$project" --env-file "$root/.env" --env-file "$dir/release.env" -f "$dir/compose.vps.yaml" "$@"
}
revision() { sed -n 's/^KRILL_REVISION=//p' "$1/release.env"; }
schema_js='const {DatabaseSync}=require("node:sqlite");const db=new DatabaseSync("storage/quiz.sqlite",{readOnly:true});console.log(db.prepare("PRAGMA user_version").get().user_version);db.close()'
compatible() {
  local dir=$1 actual
  [[ -s "$dir/schema" ]] || return 1
  actual=$(compose "$dir" run --rm --no-deps -T --pull never app node -e "$schema_js") || return 1
  [[ "$actual" == "$(cat "$dir/schema")" ]]
}
activate() {
  local dir=$1 origin expected body
  expected=$(revision "$dir")
  compose "$dir" up -d --remove-orphans --no-build --pull never --wait --wait-timeout 100 || return 1
  origin=$(compose "$dir" exec -T app node -p 'process.env.PUBLIC_ORIGIN') || return 1
  # Caddy may briefly cache the replaced container's address.
  for attempt in {1..10}; do
    if body=$(curl --silent --show-error --fail --max-time 5 "$origin/healthz") &&
       python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") is True and d.get("revision")==sys.argv[1] else 1)' "$expected" <<< "$body"; then
      return 0
    fi
    sleep 2
  done
  echo "Public health check did not report revision $expected" >&2
  return 1
}
point() {
  ln -s "$2" "$root/.$1.$$"
  mv -Tf "$root/.$1.$$" "$root/$1"
}

[[ "$(revision "$release")" =~ ^[a-f0-9]{40}$ ]] || { echo "Invalid release revision" >&2; exit 1; }
compose "$release" config --quiet
if [[ "$mode" == rollback ]] && ! compatible "$release"; then
  echo "Rollback stopped. Check the saved schema and the database recovery steps in INSTALL.md." >&2
  exit 1
fi
reader=${current:-$release}
needs_backup=false
if [[ -n "$current" ]]; then
  needs_backup=true
elif docker volume inspect "${project}_quiz_data" >/dev/null 2>&1; then
  # A failed first installation may have created the volume before SQLite started.
  probe_status=0
  compose "$reader" run --rm --no-deps -T --pull never app node -e \
    'try { require("node:fs").statSync("storage/quiz.sqlite") } catch (e) { if(e.code==="ENOENT") process.exit(42); throw e }' || probe_status=$?
  case "$probe_status" in
    0) needs_backup=true ;;
    42) ;;
    *) exit "$probe_status" ;;
  esac
fi
if [[ "$needs_backup" == true ]]; then
  if [[ "$mode" == deploy ]]; then
    while true; do
      status=0
      compose "$release" run --rm --no-deps -T --pull never app node scripts/retire-builder.mjs check || status=$?
      case "$status" in
        0) break ;;
        42) echo "Waiting for the host to reveal a legacy round."; sleep 20 ;;
        *) exit "$status" ;;
      esac
    done
  fi
  backup="before-$(basename "$release")-$(date -u +%Y%m%dT%H%M%S)-$$.sqlite"
  compose "$reader" run --rm --no-deps -T --pull never app node scripts/database.mjs backup "$backup"
  printf '%s\n' "$backup" > "$release/last-backup"
fi
verify_release() {
  local schema
  activate "$release" || return 1
  schema=$(compose "$release" exec -T app node -e "$schema_js") || return 1
  [[ "$schema" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$schema" > "$release/schema"
}
if ! verify_release; then
  compose "$release" ps --all >&2 || true
  if [[ -n "$current" && "$current" != "$release" ]] && compatible "$current"; then
    echo "Restoring the last healthy application release." >&2
    activate "$current" || echo "Recovery health check failed. See INSTALL.md." >&2
  else
    echo "Inspect the failed release and its backup before recovery. See INSTALL.md." >&2
  fi
  exit 1
fi
if [[ "$current" != "$release" ]]; then
  if [[ -n "$current" ]]; then point previous "$current"; fi
  point current "$release"
fi
if [[ "$mode" == deploy ]]; then
  compose "$release" exec -T app node scripts/retire-builder.mjs clean
  volume="${project}_knowledge_model"
  if [[ "$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}} {{index .Labels "com.docker.compose.volume"}}' "$volume" 2>/dev/null || true)" == "$project knowledge_model" ]]; then
    docker volume rm "$volume"
  fi
fi
echo "Healthy release: $(revision "$release")"
