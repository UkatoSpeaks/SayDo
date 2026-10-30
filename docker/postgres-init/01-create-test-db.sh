#!/bin/bash
# Runs once, on first start of an empty data volume.
# Creates <POSTGRES_DB>_test, owned by POSTGRES_USER.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v db="${POSTGRES_DB}_test" -v owner="$POSTGRES_USER" <<'SQL'
CREATE DATABASE :"db" OWNER :"owner";
SQL
