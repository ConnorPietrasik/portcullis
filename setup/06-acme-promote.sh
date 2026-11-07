#!/usr/bin/env bash
# Promote ACME from Let's Encrypt staging to production.
# Normally automatic at the end of 02-deploy-stack.sh once the staging cert
# has issued; this is the manual/idempotent fallback.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need docker
need python3
load_env

[[ -d /opt/panel ]] || die "/opt/panel missing — run: make deploy"

acme_promote /opt/panel
