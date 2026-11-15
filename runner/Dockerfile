FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
ARG CURSOR_VERSION=2026.11.13-eacade1
ARG CURSOR_SHA256=c02e4321f20d691ee03af94d29948dc3a81c6c34e7328da7bb1afade4c75812f
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl openssh-server python3 libstdc++6 libgcc-s1 git ripgrep \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
    && useradd --uid 10001 --create-home --shell /bin/sh cursor \
    && usermod -p '*' cursor \
    && mkdir -p /opt/cursor /opt/cursor-runner /var/lib/cursor-login /run/cursor-server
# Download the official distribution, verify the pinned artifact, never execute
# installer scripts or model requests during the build. v0.1 ships Linux amd64.
RUN curl -fLsS --retry 3 "https://downloads.cursor.com/lab/${CURSOR_VERSION}/linux/x64/agent-cli-package.tar.gz" -o /tmp/cursor.tar.gz \
    && echo "${CURSOR_SHA256}  /tmp/cursor.tar.gz" | sha256sum -c - \
    && tar --strip-components=1 -xzf /tmp/cursor.tar.gz -C /opt/cursor \
    && rm /tmp/cursor.tar.gz \
    && chmod -R a+rX /opt/cursor
COPY cursor_plugin/ /opt/cursor-runner/cursor_plugin/
COPY LICENSE NOTICE /opt/cursor-runner/
COPY licenses/ /opt/cursor-runner/licenses/
COPY runner/entry.py /opt/cursor-runner/entry.py
COPY runner/admin.py /usr/local/bin/cursor-admin
COPY runner/start.sh /usr/local/bin/cursor-start
COPY runner/sshd_config /etc/ssh/cursor_sshd_config
RUN chmod 755 /usr/local/bin/cursor-admin /usr/local/bin/cursor-start \
    && chown -R root:root /opt/cursor /opt/cursor-runner
EXPOSE 2222
ENTRYPOINT ["/usr/local/bin/cursor-start"]
