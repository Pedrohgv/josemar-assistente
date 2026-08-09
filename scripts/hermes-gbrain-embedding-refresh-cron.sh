#!/usr/bin/env bash
# Hermes no-agent cron entrypoint for the daily semantic refresh.
set -euo pipefail

# Hermes supplies HERMES_CRON_SCRIPT_TIMEOUT for the outer script deadline.
# Keep the helper's work timeout strictly below that deadline after TERM/KILL
# grace, post-KILL group drain, and a safety margin. The helper receives the
# constrained value, rather than independently guessing the outer timeout.
outer_timeout="${HERMES_CRON_SCRIPT_TIMEOUT:-300}"
requested_timeout="${GBRAIN_EMBED_REFRESH_TIMEOUT:-240}"
kill_grace="${GBRAIN_EMBED_REFRESH_KILL_GRACE:-5}"
group_drain="${GBRAIN_EMBED_REFRESH_GROUP_DRAIN:-2}"
safety_margin="${GBRAIN_EMBED_REFRESH_TIMEOUT_MARGIN:-10}"

case "$outer_timeout:$requested_timeout:$kill_grace:$group_drain:$safety_margin" in
    *[!0-9:]*|*::*|"0"*)
        echo "invalid daily embedding timeout configuration" >&2
        exit 2
        ;;
esac

safe_timeout=$((outer_timeout - kill_grace - group_drain - safety_margin - 1))
if [ "$safe_timeout" -lt 1 ]; then
    echo "HERMES_CRON_SCRIPT_TIMEOUT is too short for embedding cleanup" >&2
    exit 2
fi
if [ "$requested_timeout" -ge "$safe_timeout" ]; then
    echo "capping GBRAIN_EMBED_REFRESH_TIMEOUT to ${safe_timeout}s below Hermes outer timeout" >&2
    requested_timeout="$safe_timeout"
fi

export GBRAIN_EMBED_REFRESH_TIMEOUT="$requested_timeout"
export GBRAIN_EMBED_REFRESH_KILL_GRACE="$kill_grace"
export GBRAIN_EMBED_REFRESH_GROUP_DRAIN="$group_drain"

exec python3 "${GBRAIN_EMBED_REFRESH_HELPER:-/opt/josemar/scripts/hermes-gbrain-embedding-refresh.py}"
