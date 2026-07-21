#!/usr/bin/env bash
set -euo pipefail

lan_interface="${LAN_INTERFACE:-enp4s0}"
lan_hostname="${LAN_HOSTNAME:-evidence-studio.local}"
lan_service_name="${LAN_SERVICE_NAME:-TubeFactory}"
lan_http_port="${LAN_HTTP_PORT:-8090}"
poll_seconds="${LAN_POLL_SECONDS:-5}"

if [[ ! "$lan_hostname" =~ ^[a-z0-9][a-z0-9-]*\.local$ ]]; then
  echo "LAN_HOSTNAME must be a single DNS-safe .local name" >&2
  exit 2
fi
if [[ ! "$lan_http_port" =~ ^[0-9]+$ ]] || ((lan_http_port < 1 || lan_http_port > 65535)); then
  echo "LAN_HTTP_PORT must be between 1 and 65535" >&2
  exit 2
fi
if ! ip link show dev "$lan_interface" >/dev/null 2>&1; then
  echo "LAN interface does not exist: $lan_interface" >&2
  exit 2
fi
for command_name in avahi-publish-address avahi-publish-service ip; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is missing: $command_name" >&2
    exit 2
  fi
done

address_pid=""
service_pid=""
published_ip=""

stop_publishers() {
  local pid
  for pid in "$service_pid" "$address_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  address_pid=""
  service_pid=""
}

cleanup() {
  stop_publishers
}
trap cleanup EXIT INT TERM

current_ipv4() {
  ip -4 -o address show dev "$lan_interface" scope global \
    | awk '{split($4, address, "/"); print address[1]; exit}'
}

while true; do
  next_ip="$(current_ipv4)"
  if [[ -z "$next_ip" ]]; then
    if [[ -n "$published_ip" ]]; then
      stop_publishers
      published_ip=""
    fi
    sleep "$poll_seconds"
    continue
  fi

  if [[ "$next_ip" != "$published_ip" ]] \
    || [[ -z "$address_pid" ]] || ! kill -0 "$address_pid" 2>/dev/null \
    || [[ -z "$service_pid" ]] || ! kill -0 "$service_pid" 2>/dev/null; then
    stop_publishers
    avahi-publish-address --no-reverse "$lan_hostname" "$next_ip" &
    address_pid=$!
    sleep 1
    avahi-publish-service --host="$lan_hostname" "$lan_service_name" _http._tcp "$lan_http_port" \
      "path=/" "product=TubeFactory" &
    service_pid=$!
    published_ip="$next_ip"
    echo "Published $lan_hostname -> $published_ip and _http._tcp:$lan_http_port"
  fi

  sleep "$poll_seconds"
done
