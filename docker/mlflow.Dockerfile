FROM ghcr.io/mlflow/mlflow:v3.14.0

RUN pip install --no-cache-dir psycopg2-binary==2.9.9

RUN addgroup --system app && adduser --system --ingroup app --home /tmp app

ENV XDG_CACHE_HOME=/tmp/.cache

USER app
