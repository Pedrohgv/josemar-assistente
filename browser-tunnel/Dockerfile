# Browser tunnel sidecar image.
#
# Hardened OpenSSH reverse-tunnel endpoint for the optional browser-control
# overlay. The container shares Hermes's network namespace
# (network_mode: service:hermes in docker-compose.browser-control.yml) and the
# sshd daemon binds only to the browser-control interface IP
# (BROWSER_CONTROL_HERMES_IP), port 2222. A laptop connects over the existing
# Tailscale node (Tailscale Serve forwards tcp:2222 to
# BROWSER_CONTROL_HERMES_IP:2222) and creates a reverse forward
# `-R 127.0.0.1:9222:127.0.0.1:9222`; because the sidecar shares Hermes's
# namespace, Hermes reaches Chrome CDP at 127.0.0.1:9222.
#
# Constants (not configurable): SSH user `tunnel`, SSH port 2222, CDP port 9222.
# See docs/browser-control.md for the full threat model and laptop setup.
#
# Pinned to Alpine 3.20 to match the project's existing Alpine usage in the
# deploy workflow. Update the patch tag deliberately when refreshing.

FROM alpine:3.20.3

RUN apk add --no-cache openssh-server=9.7_p1-r5 \
    && mkdir -p /var/lib/browser-tunnel /etc/ssh/runtime /run /tmp \
    && chmod 1777 /run /tmp

# Non-root, passwordless, unlocked forwarding-only user. Shell is /bin/false so
# no interactive shell is possible; the account is unlocked with an empty
# password (passwd -d) so sshd's privilege-separation can transition to it.
# Password authentication is disabled globally in sshd_config, so the empty
# password cannot be used to log in. Home is /home/tunnel (NOT the host-key
# volume path) so the browser-tunnel-state volume stays root-owned and the
# entrypoint can chmod it without DAC_OVERRIDE/FOWNER caps.
RUN addgroup -S tunnel \
    && adduser -S -G tunnel -h /home/tunnel -s /bin/false tunnel \
    && passwd -d tunnel

COPY entrypoint.sh /usr/local/bin/browser-tunnel-entrypoint
COPY sshd_config.template /etc/ssh/sshd_config.template

RUN chmod 0755 /usr/local/bin/browser-tunnel-entrypoint \
    && chmod 0644 /etc/ssh/sshd_config.template

ENTRYPOINT ["/usr/local/bin/browser-tunnel-entrypoint"]