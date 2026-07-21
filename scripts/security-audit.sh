#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_json=$(mktemp)
trap 'rm -f "$compose_json"' EXIT
docker compose -f "$repo_root/compose.yaml" --profile '*' config --format json > "$compose_json"

published=$(jq -r '
  [.services | to_entries[] | .key as $service | .value.ports[]? |
   select((.published // "") != "") | "\($service):\(.published)->\(.target)"] | .[]
' "$compose_json")
if [[ "$published" != "caddy:${HTTP_PORT:-8090}->80" ]]; then
  echo "unexpected host port exposure:" >&2
  echo "$published" >&2
  exit 1
fi

jq -e '
  [.services | to_entries[] |
   select(.key | IN("web","api","workflow-worker","editorial-worker","research-worker","publisher-worker","render-service")) |
   select(
     (.value.read_only // false) != true or
     ((.value.security_opt // []) | index("no-new-privileges:true") | not) or
     ((.value.cap_drop // []) | index("ALL") | not) or
     (.value.stop_grace_period // "") == ""
   )] |
  length == 0
' "$compose_json" >/dev/null

jq -e '
  [.networks | to_entries[] |
   select(.key | IN("application","data","firecrawl-backend","firecrawl-client","observability")) |
   select((.value.internal // false) != true)] | length == 0
' "$compose_json" >/dev/null

egress_members=$(docker network inspect youtuber-publishing-egress --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null || true)
if [[ -n "$egress_members" && "$egress_members" != "youtuber-publisher-worker-1 " ]]; then
  echo "publishing egress contains an unexpected container: $egress_members" >&2
  exit 1
fi

identity_egress_members=$(docker network inspect youtuber-identity-egress --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null || true)
if [[ -n "$identity_egress_members" && "$identity_egress_members" != "youtuber-api-1 " ]]; then
  echo "identity egress contains an unexpected container: $identity_egress_members" >&2
  exit 1
fi

provider_egress_members=$(docker network inspect youtuber-provider-egress --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null || true)
if [[ -n "$provider_egress_members" && "$provider_egress_members" != "youtuber-editorial-worker-1 " ]]; then
  echo "provider egress contains an unexpected container: $provider_egress_members" >&2
  exit 1
fi

if [[ ${APP_ENVIRONMENT:-development} == production ]]; then
  if grep -Rqs 'development-only' "$repo_root/infra/compose/secrets"; then
    echo "production audit failed: development sentinel secrets are still mounted" >&2
    exit 1
  fi
  if [[ ${COOKIE_SECURE:-false} != true || ${APP_SITE_ADDRESS:-} != https://* ]]; then
    echo "production audit failed: HTTPS and secure cookies are mandatory" >&2
    exit 1
  fi
fi

echo '{"result":"passed","host_ports":["caddy:'"${HTTP_PORT:-8090}"'->80"],"application_filesystems":"read_only","capabilities_dropped":"ALL","no_new_privileges":true,"graceful_shutdown_configured":true,"internal_networks_verified":true,"publisher_egress_isolated":true,"provider_egress_isolated":true,"identity_egress_isolated":true}'
