.PHONY: test lint deploy delete

STACK ?= claude-code-finops

test:
	python3 -m pytest tests/ -q

lint:
	cfn-lint template.yaml

# Prompts for tokens/budgets on first run; saves choices to samconfig.toml
deploy:
	sam deploy --stack-name $(STACK) --guided --capabilities CAPABILITY_IAM

delete:
	sam delete --stack-name $(STACK)
