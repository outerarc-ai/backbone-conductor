ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/backbone
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && git config --system user.name 'Backbone Conductor' \
    && git config --system user.email 'backbone@localhost' \
    && git config --system safe.directory /workspace
WORKDIR /workspace
ENTRYPOINT ["backbone"]
CMD ["--repo", "/workspace", "serve", "--host", "0.0.0.0", "--port", "8000", "--auth-file", "/run/secrets/backbone-http-tokens.json"]
