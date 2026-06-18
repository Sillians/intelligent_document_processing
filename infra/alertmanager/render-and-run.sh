#!/bin/sh
set -eu

escape_sed() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

: "${ALERT_EMAIL_TO:?ALERT_EMAIL_TO is required}"
: "${ALERT_EMAIL_FROM:?ALERT_EMAIL_FROM is required}"
: "${ALERT_EMAIL_SMARTHOST:?ALERT_EMAIL_SMARTHOST is required}"
: "${ALERT_EMAIL_USERNAME:?ALERT_EMAIL_USERNAME is required}"
: "${ALERT_EMAIL_REQUIRE_TLS:=true}"
: "${ALERT_EMAIL_REPEAT_INTERVAL:=4h}"

sed \
  -e "s|__ALERT_EMAIL_SMARTHOST__|$(escape_sed "$ALERT_EMAIL_SMARTHOST")|g" \
  -e "s|__ALERT_EMAIL_FROM__|$(escape_sed "$ALERT_EMAIL_FROM")|g" \
  -e "s|__ALERT_EMAIL_USERNAME__|$(escape_sed "$ALERT_EMAIL_USERNAME")|g" \
  -e "s|__ALERT_EMAIL_PASSWORD__|$(escape_sed "${ALERT_EMAIL_PASSWORD:-}")|g" \
  -e "s|__ALERT_EMAIL_REQUIRE_TLS__|$(escape_sed "$ALERT_EMAIL_REQUIRE_TLS")|g" \
  -e "s|__ALERT_EMAIL_REPEAT_INTERVAL__|$(escape_sed "$ALERT_EMAIL_REPEAT_INTERVAL")|g" \
  -e "s|__ALERT_EMAIL_TO__|$(escape_sed "$ALERT_EMAIL_TO")|g" \
  /etc/alertmanager/alertmanager.yml.template > /tmp/alertmanager.yml

exec /bin/alertmanager \
  --config.file=/tmp/alertmanager.yml \
  --storage.path=/alertmanager \
  --web.external-url=http://localhost:9093
