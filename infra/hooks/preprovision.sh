#!/bin/sh
# Generate a Postgres admin password once per azd environment (stored in .azure/<env>/.env, not in git).
set -e
if [ -z "$(azd env get-value POSTGRES_PASSWORD 2>/dev/null || true)" ]; then
  azd env set POSTGRES_PASSWORD "$(openssl rand -base64 30 | tr -d '/+=' | cut -c1-32)Aa1"
fi
