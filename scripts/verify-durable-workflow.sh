#!/usr/bin/env bash
set -euo pipefail

base_url="${BASE_URL:-http://localhost:8090}"
username="${GATE_USERNAME:?Set GATE_USERNAME to an operator or admin username}"
password="${GATE_PASSWORD:?Set GATE_PASSWORD to that account password}"
cookie_file="$(mktemp)"
trap 'rm -f "$cookie_file"' EXIT

for dependency in curl jq docker; do
  command -v "$dependency" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$dependency" >&2
    exit 1
  }
done

if docker info >/dev/null 2>&1; then
  docker_command=(docker)
elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
  docker_command=(sudo -n docker)
else
  printf 'Docker is unavailable; grant this user Docker access or passwordless sudo for Docker\n' >&2
  exit 1
fi

login_body="$(
  curl --fail --silent --show-error \
    --cookie-jar "$cookie_file" \
    --header 'Content-Type: application/json' \
    --data "$(jq -n --arg username "$username" --arg password "$password" '{username:$username,password:$password}')" \
    "${base_url}/api/v1/auth/login"
)"
csrf_token="$(printf '%s' "$login_body" | jq -er '.csrf_token')"
idempotency_key="gate-$(date -u +%Y%m%dT%H%M%S)-$$"

probe_body="$(
  curl --fail --silent --show-error \
    --cookie "$cookie_file" \
    --header "X-CSRF-Token: ${csrf_token}" \
    --header 'Content-Type: application/json' \
    --data "$(jq -n --arg key "$idempotency_key" '{idempotency_key:$key}')" \
    "${base_url}/api/v1/system/durability-probes"
)"
workflow_id="$(printf '%s' "$probe_body" | jq -er '.workflow_id')"
started_at="$(printf '%s' "$probe_body" | jq -er '.started_at')"
printf '%s' "$probe_body" | jq -e '.state == "WAITING"' >/dev/null

worker_container="${WORKFLOW_WORKER_CONTAINER:-youtuber-workflow-worker-1}"
"${docker_command[@]}" restart --timeout 10 "$worker_container" >/dev/null
for _ in $(seq 1 30); do
  worker_status="$("${docker_command[@]}" inspect --format '{{.State.Health.Status}}' "$worker_container")"
  test "$worker_status" = "healthy" && break
  sleep 2
done
test "${worker_status:-unknown}" = "healthy"

after_restart=""
for _ in $(seq 1 30); do
  if after_restart="$(
    curl --fail --silent --show-error --cookie "$cookie_file" \
      "${base_url}/api/v1/system/durability-probes/${workflow_id}" 2>/dev/null
  )"; then
    break
  fi
  sleep 2
done
test -n "$after_restart"
printf '%s' "$after_restart" | jq -e \
  --arg workflow_id "$workflow_id" --arg started_at "$started_at" \
  '.workflow_id == $workflow_id and .started_at == $started_at and .state == "WAITING"' >/dev/null

completed="$(
  curl --fail --silent --show-error \
    --cookie "$cookie_file" \
    --header "X-CSRF-Token: ${csrf_token}" \
    --request POST \
    "${base_url}/api/v1/system/durability-probes/${workflow_id}/complete"
)"
printf '%s' "$completed" | jq -e '.state == "COMPLETED" and .completed_at != null' >/dev/null

# Retry the same completion to prove reconciliation is idempotent.
curl --fail --silent --show-error \
  --cookie "$cookie_file" \
  --header "X-CSRF-Token: ${csrf_token}" \
  --request POST \
  "${base_url}/api/v1/system/durability-probes/${workflow_id}/complete" \
  | jq -e '.state == "COMPLETED"' >/dev/null

printf 'durable workflow gate passed: %s survived worker restart and completion retry\n' "$workflow_id"
