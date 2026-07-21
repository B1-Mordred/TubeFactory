#!/usr/bin/env bash
set -euo pipefail

base_url="${BASE_URL:-http://localhost:8090}"
username="${GATE_USERNAME:?Set GATE_USERNAME to an operator or admin username}"
password="${GATE_PASSWORD:?Set GATE_PASSWORD to that account password}"

for dependency in curl jq; do
  command -v "$dependency" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$dependency" >&2
    exit 1
  }
done

cookie_file="$(mktemp)"
trap 'rm -f "$cookie_file"' EXIT
login="$(
  curl --fail --silent --show-error --cookie-jar "$cookie_file" \
    --header 'Content-Type: application/json' \
    --data "$(jq -n --arg username "$username" --arg password "$password" '{username:$username,password:$password}')" \
    "${base_url}/api/v1/auth/login"
)"
csrf="$(printf '%s' "$login" | jq -er .csrf_token)"
suffix="$(date -u +%Y%m%dT%H%M%S)-$$"

channel="$(
  jq -n --arg slug "fixture-${suffix,,}" '{
    slug:$slug,name:"Acceptance Fixture Channel",enabled:true,
    identity:{purpose:"deterministic acceptance"},languages:["en"],audience:{},
    editorial_rules:{evidence_first:true},brand_kit:{},default_render_settings:{},
    default_publish_settings:{privacy:"private",automatic_publication:false}
  }' | curl --fail --silent --show-error --cookie "$cookie_file" \
    --header "X-CSRF-Token: ${csrf}" --header 'Content-Type: application/json' \
    --data @- "${base_url}/api/v1/channel-profiles"
)"
channel_id="$(printf '%s' "$channel" | jq -er .id)"

subject="$(
  jq -n --arg channel "$channel_id" '{
    channel_profile_id:$channel,name:"Deterministic evidence fixture",enabled:true,
    topic:"A measured value changed during 2025",research_goal:"Separate measurement from causal interpretation",
    excluded_angles:[],seed_queries:["measured value 2025 evidence"],related_concepts:["measurement","causality"],
    negative_keywords:["advertisement"],languages:["en"],regions:[],domain_policy:{allow:[],block:[]},
    source_requirements:{minimum_independent:2,minimum_primary:1,expected_primary_types:["official record","original study"]},
    schedule:{cron:null,timezone:"UTC"},freshness_policy:{lookback_days:30,maximum_source_age_days:3650},
    format_policy:{target:"standard",duration_seconds:600},editorial_profile:{tone:"calm"},risk:"high",
    budget:{tokens:0,gpu_seconds:0,currency_minor:0},opportunity_weights:{},approval_profile:{dossier:"reviewer"}
  }' | curl --fail --silent --show-error --cookie "$cookie_file" \
    --header "X-CSRF-Token: ${csrf}" --header 'Content-Type: application/json' \
    --data @- "${base_url}/api/v1/subject-profiles"
)"
subject_id="$(printf '%s' "$subject" | jq -er .id)"

plan="$(
  curl --fail --silent --show-error --cookie "$cookie_file" \
    --header "X-CSRF-Token: ${csrf}" --request POST \
    "${base_url}/api/v1/subject-profiles/${subject_id}/test-search-plan"
)"
printf '%s' "$plan" | jq -e '.strategies | length >= 3' >/dev/null
printf '%s' "$plan" | jq -e '.falsification_queries | length >= 1' >/dev/null

start="$(
  jq -n --arg subject "$subject_id" --arg key "fixture-${suffix}" \
    '{subject_profile_id:$subject,idempotency_key:$key}' \
  | curl --fail --silent --show-error --cookie "$cookie_file" \
      --header "X-CSRF-Token: ${csrf}" --header 'Content-Type: application/json' \
      --data @- "${base_url}/api/v1/research/fixture-runs"
)"
workflow_id="$(printf '%s' "$start" | jq -er .workflow_id)"

status=""
for _ in $(seq 1 60); do
  status="$(curl --fail --silent --show-error --cookie "$cookie_file" "${base_url}/api/v1/research/runs/${workflow_id}")"
  test "$(printf '%s' "$status" | jq -r .state)" = "DOSSIER_REVIEW" && break
  sleep 1
done
printf '%s' "$status" | jq -e '
  .state == "DOSSIER_REVIEW" and .progress == 100 and
  .result.source_count == 3 and .result.deduplicated_search_results == 1 and
  (.result.score >= 0 and .result.score <= 100)
' >/dev/null
dossier_id="$(printf '%s' "$status" | jq -er .result.dossier_id)"
dossier="$(curl --fail --silent --show-error --cookie "$cookie_file" "${base_url}/api/v1/research/dossiers/${dossier_id}")"
printf '%s' "$dossier" | jq -e '
  .status == "in_review" and .completion_evaluation.complete == true and
  ([.claims[].evidence[].relationship] | index("supports") != null) and
  ([.claims[].evidence[].relationship] | index("contradicts") != null) and
  ([.claims[].evidence[].snapshot.content_hash | length == 64] | all)
' >/dev/null

before="$(curl --fail --silent --show-error --cookie "$cookie_file" "${base_url}/api/v1/research/dossiers" | jq 'length')"
retry="$(
  jq -n --arg subject "$subject_id" --arg key "fixture-${suffix}" \
    '{subject_profile_id:$subject,idempotency_key:$key}' \
  | curl --fail --silent --show-error --cookie "$cookie_file" \
      --header "X-CSRF-Token: ${csrf}" --header 'Content-Type: application/json' \
      --data @- "${base_url}/api/v1/research/fixture-runs"
)"
printf '%s' "$retry" | jq -e --arg workflow "$workflow_id" '.workflow_id == $workflow' >/dev/null
after="$(curl --fail --silent --show-error --cookie "$cookie_file" "${base_url}/api/v1/research/dossiers" | jq 'length')"
test "$before" = "$after"

printf 'discovery fixture gate passed: %s produced dossier %s with traceable support and contradiction\n' \
  "$workflow_id" "$dossier_id"
