SHELL := /bin/bash
PY ?= python3
DEMO_DIR ?= demo
ANSIBLE_DIR := ansible

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: test
test: ## Run the unit tests
	$(PY) -m unittest discover -s tests -v

.PHONY: lint
lint: ## Lint Python and Ansible
	ruff check .
	ansible-lint $(ANSIBLE_DIR)/

.PHONY: syntax
syntax: ## Syntax-check the playbooks
	cd $(ANSIBLE_DIR) && ansible-playbook site.yml collect.yml uninstall.yml \
		-i inventory.example.ini --syntax-check

.PHONY: demo
demo: ## Generate a synthetic fleet and print the report
	$(PY) tools/demo_data.py --out $(DEMO_DIR) --hosts 4 --days 30
	$(PY) monitor_fs.py --data-dir $(DEMO_DIR) report --days 30 --json $(DEMO_DIR)/report.json
	$(PY) fleet_report.py $(DEMO_DIR)/report.json --markdown $(DEMO_DIR)/FLEET.md

.PHONY: collect
collect: ## Collect on this host (writes to ./local-data)
	$(PY) monitor_fs.py --data-dir ./local-data collect

.PHONY: report
report: ## Report on this host's collected data
	$(PY) monitor_fs.py --data-dir ./local-data report --days 30

.PHONY: deploy
deploy: ## Deploy to the fleet (ansible/inventory.ini)
	cd $(ANSIBLE_DIR) && ansible-playbook site.yml

.PHONY: fleet
fleet: ## Gather reports from the fleet and roll them up
	cd $(ANSIBLE_DIR) && ansible-playbook collect.yml

.PHONY: dev-deps
dev-deps: ## Install lint/test tooling (the collector itself has no dependencies)
	$(PY) -m pip install -r requirements-dev.txt

.PHONY: clean
clean: ## Remove generated artifacts
	rm -rf $(DEMO_DIR) local-data ci-data ansible/reports .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
