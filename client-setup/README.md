# Pointing Claude Code at the pipeline

Every developer (or your managed settings) needs five environment
variables. Get `IngestEndpoint` from the stack outputs and the ingest
token you chose at deploy time.

## Shell profile (individual setup)

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=https://<api-id>.execute-api.<region>.amazonaws.com
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <your-ingest-token>"

# strongly recommended: delta temporality makes ingest exact
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta

# team attribution (shows up as the T# rollup and per-team budgets)
export OTEL_RESOURCE_ATTRIBUTES="team.id=platform,department=engineering"
```

Notes:

- `http/json` is required -- the ingest Lambda parses OTLP JSON, not
  protobuf or gRPC.
- Cumulative temporality also works (the pipeline converts it using
  per-session state), but delta is exact and cheaper. Set it.
- `team.id` is what the per-team rollups, budgets, and alerts key on.
  Also accepted: `team`, `department`, `cost_center` (first match wins).

## Managed settings (org-wide rollout)

Drop into your managed settings file (e.g.
`/Library/Application Support/ClaudeCode/managed-settings.json` on
macOS, `/etc/claude-code/managed-settings.json` on Linux) so every
developer reports automatically:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://<api-id>.execute-api.<region>.amazonaws.com",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer <your-ingest-token>",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta"
  }
}
```

Set `OTEL_RESOURCE_ATTRIBUTES` per team (per-repo `.claude/settings.json`
works well: the repo knows which team owns it).

## Verifying

Run any Claude Code session, wait one export interval (60s by default),
then open the dashboard. If nothing arrives:

- `OTEL_METRIC_EXPORT_INTERVAL=5000` temporarily, to export every 5s
- `OTEL_METRICS_EXPORTER=console` to see what the CLI would send
- check the IngestFunction CloudWatch logs for 401s (token mismatch)
