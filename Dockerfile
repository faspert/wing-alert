FROM docker.io/library/eclipse-temurin:25-jre-noble
ARG SIGNAL_CLI_VERSION=0.14.5

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SIGNAL_CONFIG_DIR=/var/lib/signal-cli

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
       python3 \
       python3-pip \
       python3-venv \
    && curl -L \
       "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
       -o /tmp/signal-cli.tar.gz \
    && tar xf /tmp/signal-cli.tar.gz -C /opt \
    && ln -s \
       /opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli \
       /usr/local/bin/signal-cli \
    && rm /tmp/signal-cli.tar.gz \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY notifications.py wing-alert-grid.py /app/

WORKDIR /data

# /data stores forecast state, downloaded cache, and generated plots.
# /var/lib/signal-cli must be populated with a registered signal-cli account.
VOLUME ["/data", "/var/lib/signal-cli"]

ENTRYPOINT ["/opt/venv/bin/python", "/app/wing-alert-grid.py"]
