#!/usr/bin/env bash
# Hermes no-agent cron entrypoint for the daily semantic refresh.
set -euo pipefail

# The lock and gbrain must never be touched as root: enforce the hermes
# runtime identity here instead of relying on base-image behavior. Fail
# closed — an unknown/garbage UID also refuses.
uid="$(/usr/bin/id -u 2>/dev/null)" || {
    echo "gbrain embedding refresh cron: could not determine the effective UID; refusing to run" >&2
    exit 1
}
case "$uid" in
    ""|*[!0-9]*)
        echo "gbrain embedding refresh cron: could not determine the effective UID; refusing to run" >&2
        exit 1
        ;;
esac
if [ "$uid" = "0" ]; then
    echo "gbrain embedding refresh cron: refuses to run as root; run as the hermes runtime user" >&2
    exit 1
fi

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

export HERMES_CRON_SCRIPT_TIMEOUT
export GBRAIN_EMBED_REFRESH_TIMEOUT="$requested_timeout"
export GBRAIN_EMBED_REFRESH_KILL_GRACE="$kill_grace"
export GBRAIN_EMBED_REFRESH_GROUP_DRAIN="$group_drain"

# Fixed image interpreter in isolated mode and immutable helper path: the
# cron environment cannot redirect the helper or inject code before it runs.
exec "/opt/hermes/.venv/bin/python3" -I "/opt/josemar/scripts/hermes-gbrain-embedding-refresh.py"
