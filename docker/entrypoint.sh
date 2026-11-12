#!/bin/sh
# Starts the app as PUID:PGID. Root is only used to apply those ids and to fix ownership of the
# data directory; the web server never runs as root.
# One uvicorn process only: the scheduler and live pub/sub live in-process, never add --workers.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
PUID="${PUID:-99}"
PGID="${PGID:-100}"

set -- uvicorn app.main:app --host 0.0.0.0 --port 8080 \
  --proxy-headers --forwarded-allow-ips '*' --no-server-header

is_positive_number() {
  case "$1" in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "$1" -gt 0 ]
}

if [ "$(id -u)" != "0" ]; then
  # Started with --user: the ids cannot be changed, the volume must already be writable.
  exec "$@"
fi

if ! is_positive_number "$PUID" || ! is_positive_number "$PGID"; then
  echo "PUID and PGID must be numbers greater than 0" >&2
  exit 1
fi

groupmod --non-unique --gid "$PGID" app
usermod --non-unique --uid "$PUID" --gid "$PGID" app

mkdir -p "$DATA_DIR"
if [ "$(stat -c '%u:%g' "$DATA_DIR")" != "$PUID:$PGID" ]; then
  chown -R "$PUID:$PGID" "$DATA_DIR"
fi

exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
