# claude-code-finops

Serverless FinOps pipeline for Claude Code usage on Amazon Bedrock:
OTLP ingest -> DynamoDB rollups + S3 raw archive -> dashboard + alerts.

## Layout

- `template.yaml`: SAM/CloudFormation, the whole stack
- `src/shared/`: OTLP parsing (`otlp.py`), rollup logic (`rollup.py`), pricing (`pricing.py`); pure logic, no AWS calls
- `src/ingest/`: POST /v1/metrics handler (auth, archive, rollups)
- `src/api/`: dashboard API + serves `src/api/static/index.html`
- `src/alerts/`: hourly budget/spike checks -> SNS
- `tests/`: pytest, no network/moto; fakes live in `test_handlers.py`

## Conventions

- Keep AWS calls out of `src/shared/` so logic stays unit-testable.
- DynamoDB rollup schema is documented at the top of `src/shared/rollup.py`;
  change it there first, then update readers (api, alerts, reconcile).
- The dashboard is a single self-contained HTML file: no build step,
  no external requests. Palette/mark conventions are annotated inline.
- Run `make test` and `make lint` before committing.
