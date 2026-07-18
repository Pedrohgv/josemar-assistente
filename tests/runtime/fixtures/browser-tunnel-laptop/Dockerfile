# Test-only fixture image for the browser-tunnel runtime integration test.
#
# This is NOT a production image. It simulates a laptop that runs:
#   - a mock Chrome CDP HTTP endpoint on 127.0.0.1:9222 (Python http.server)
#   - the real OpenSSH client that establishes `ssh -N -R 127.0.0.1:9222:...`
#
# All package installation happens at BUILD time so test startup is
# deterministic and build failures surface with captured stderr instead of a
# silent readiness timeout. Pinned to Alpine 3.20.3 + openssh-client 9.7_p1-r5
# to match the browser-tunnel image pins where practical.
#
# Used by tests/runtime/test_browser_tunnel_runtime.py only.

FROM alpine:3.20.3

RUN apk add --no-cache \
        openssh-client=9.7_p1-r5 \
        openssh-client-common=9.7_p1-r5 \
        openssh-client-default=9.7_p1-r5 \
        openssh-keygen=9.7_p1-r5 \
        python3=3.12.13-r0 \
    && mkdir -p /srv

COPY mock_cdp_server.py /srv/mock_cdp_server.py
RUN chmod 0755 /srv/mock_cdp_server.py

# No entrypoint: the test starts the mock server and ssh client via `docker exec`.
CMD ["sleep", "infinity"]