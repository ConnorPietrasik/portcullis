.PHONY: preflight secrets deploy api panel-do ddns promote stack status logs verify doctor update rollback

preflight:
	bash setup/00-preflight.sh

secrets:
	bash setup/01-secrets.sh

deploy:
	bash setup/02-deploy-stack.sh

api:
	bash setup/03-panel-api.sh

panel-do:
	bash setup/04-panel-do.sh

ddns:
	bash setup/05-ddns.sh

## flip ACME staging → prod (idempotent; normally automatic after make deploy)
promote:
	bash setup/06-acme-promote.sh

## stack = the README quickstart sequence (phases 2–4, RUNBOOK order) in one
## shot, for RE-RUNS. First deploy follows the RUNBOOK step by step so UFW
## lands between deploy and api (make api is gated on it and stops the chain).
stack:
	bash setup/02-deploy-stack.sh
	bash setup/05-ddns.sh
	bash setup/04-panel-do.sh
	bash setup/03-panel-api.sh

status:
	cd /opt/panel && sudo docker compose ps

logs:
	cd /opt/panel && sudo docker compose logs -f --tail=100

## post-deploy smoke test — every RUNBOOK check as one pass/fail report (read-only)
doctor:
	bash setup/07-doctor.sh

## update: the ONE Day-2 command — sync the repo's configs (compose/traefik/
## authelia/.env) AND float the whole stack to the newest release within its
## lines (container images, panel-web npm deps, pulled bases, panel-api pip
## deps). Doctor precondition, snapshot, change, gate on health + full doctor,
## auto-rollback on failure
update:
	bash setup/08-update.sh

## rollback: restore the edge stack to the snapshot taken at the last `make update`
rollback:
	bash setup/09-rollback.sh

## local sanity checks (no server needed): shell syntax + python syntax
verify:
	@set -e; for f in setup/*.sh; do bash -n "$$f"; done
	@python3 -m py_compile panel-api/main.py ddns/ddns.py
	@rm -rf panel-api/__pycache__ ddns/__pycache__  # py_compile always writes bytecode — don't litter
	@echo "verify: ok"
