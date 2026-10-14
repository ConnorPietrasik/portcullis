.PHONY: preflight secrets deploy api panel-do ddns promote stack status logs verify doctor

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

## stack = full server install, in dependency order
stack:
	bash setup/02-deploy-stack.sh
	bash setup/04-panel-do.sh
	bash setup/03-panel-api.sh
	bash setup/05-ddns.sh

status:
	cd /opt/panel && sudo docker compose ps

logs:
	cd /opt/panel && sudo docker compose logs -f --tail=100

## post-deploy smoke test — every RUNBOOK check as one pass/fail report (read-only)
doctor:
	bash setup/07-doctor.sh

## local sanity checks (no server needed): shell syntax + python syntax
verify:
	@set -e; for f in setup/*.sh; do bash -n "$$f"; done
	@python3 -m py_compile panel-api/main.py ddns/ddns.py
	@rm -rf panel-api/__pycache__ ddns/__pycache__  # py_compile always writes bytecode — don't litter
	@echo "verify: ok"
