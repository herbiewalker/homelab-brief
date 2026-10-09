# syntax=docker/dockerfile:1
# The single homelab-brief image: Vector + worker + web server under s6-overlay.
# Versions and licenses are recorded in docs/DECISIONS.md. Bump them there too.
ARG PYTHON_IMAGE=python:3.13.16-slim-bookworm

# ---- s6-overlay (ISC), x86_64 only: Unraid on x86. Add aarch64 hashes if ever needed. ----
FROM ${PYTHON_IMAGE} AS s6
RUN apt-get update \
    && apt-get install -y --no-install-recommends xz-utils \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12 \
    https://github.com/just-containers/s6-overlay/releases/download/v3.2.3.2/s6-overlay-noarch.tar.xz /tmp/
ADD --checksum=sha256:e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408 \
    https://github.com/just-containers/s6-overlay/releases/download/v3.2.3.2/s6-overlay-x86_64.tar.xz /tmp/
RUN mkdir /s6 \
    && tar -C /s6 -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C /s6 -Jxpf /tmp/s6-overlay-x86_64.tar.xz

# ---- Vector (MPL-2.0), static musl build so it runs on any base image ----
FROM ${PYTHON_IMAGE} AS vector
ADD --checksum=sha256:ad013ddc164b80e425cc403d2174e26b811673846d1125c80bc7b5024826ce39 \
    https://github.com/vectordotdev/vector/releases/download/v0.58.0/vector-0.58.0-x86_64-unknown-linux-musl.tar.gz /tmp/vector.tar.gz
RUN mkdir /tmp/v /out \
    && tar -xzf /tmp/vector.tar.gz -C /tmp/v \
    && cp "$(find /tmp/v -type f -name vector -path '*/bin/*')" /out/vector

# ---- Python dependencies in a venv ----
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY brief/requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

# ---- Tests: docker build --target test . (or: docker compose run --rm test) ----
FROM deps AS test
COPY brief/requirements-dev.txt /tmp/requirements-dev.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements-dev.txt
# DejaVu Sans Mono (Bitstream Vera licence, free to bundle) for status.png; render.py falls back to
# Pillow's built-in font if it is missing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY brief/ /app/
COPY vector/ /vector/
CMD ["pytest", "-q"]

# ---- Runtime image (default target) ----
FROM ${PYTHON_IMAGE} AS runtime
COPY --from=s6 /s6/ /
COPY --from=vector /out/vector /usr/local/bin/vector
COPY --from=deps /opt/venv /opt/venv
# DejaVu Sans Mono (Bitstream Vera licence, free to bundle) for status.png; render.py falls back to
# Pillow's built-in font if it is missing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Command line tools for the optional SNMP and iDRAC collectors (Task 22): net-snmp (BSD-style licences) for
# SNMPv3 and v2c, ipmitool (BSD-3-Clause) for IPMI-over-LAN, the only way an older iDRAC can be read. Only
# read commands are ever run; credentials are never put on a command line (see collectors/snmp.py, idrac.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends snmp ipmitool \
    && rm -rf /var/lib/apt/lists/*

# The optional SSH server (OpenBSD licence family). Off unless SSH_ENABLED is true; see brief/sshcfg.py.
# The host keys the package makes at install are removed: every container makes its own, kept in /state/ssh.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
    && mkdir -p /run/sshd
# uid 99 / gid 100 is Unraid's nobody:users, so appdata written here stays manageable from the host.
# Docker sets net.ipv4.ip_unprivileged_port_start=0 in container namespaces, so this user can bind 80/443/514.
# usermod -p '*': no password, but not "locked": sshd (without PAM) refuses a locked account even for a key.
RUN useradd --uid 99 --gid 100 --home-dir /home/brief --create-home --shell /bin/bash brief \
    && usermod -p '*' brief \
    && mkdir -p /config /state /archive /out

COPY docker/rootfs/ /
COPY brief/src/ /app/
COPY vector/ /app/vector/
COPY brief/*.yaml brief/*.ini /app/defaults/brief/
COPY brief/prompts/ /app/defaults/brief/prompts/
COPY brief/fixtures/ /app/defaults/brief/fixtures/
RUN chmod +x /etc/s6-overlay/scripts/* /etc/s6-overlay/s6-rc.d/*/run /usr/local/bin/brief

ENV PATH=/opt/venv/bin:/command:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    HTTP_PORT=80

EXPOSE 514/udp 514/tcp 80 443

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "brief.healthcheck"]

ENTRYPOINT ["/init"]
