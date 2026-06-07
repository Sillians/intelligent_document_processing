FROM python:3.11-slim

ARG UV_GROUP=base
ARG EXTRA_APT_PACKAGES=""
COPY --from=ghcr.io/astral-sh/uv:0.9.4 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app

RUN if [ -n "${EXTRA_APT_PACKAGES}" ]; then \
      apt-get update && apt-get install -y --no-install-recommends ${EXTRA_APT_PACKAGES} && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project --no-default-groups --group ${UV_GROUP}

COPY . /app
