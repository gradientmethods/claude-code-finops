"""Parser for OTLP/HTTP JSON metric payloads emitted by Claude Code.

Claude Code exports (with CLAUDE_CODE_ENABLE_TELEMETRY=1 and
OTEL_EXPORTER_OTLP_PROTOCOL=http/json) OTLP ExportMetricsServiceRequest
JSON. This module flattens the payload into normalized datapoint dicts
that the ingest handler rolls up.

Metrics consumed (anything else is passed through as raw archive only):
  claude_code.token.usage        attrs: type=input|output|cacheRead|cacheCreation, model
  claude_code.cost.usage         attrs: model
  claude_code.session.count
  claude_code.lines_of_code.count  attrs: type=added|removed
  claude_code.commit.count
  claude_code.pull_request.count
  claude_code.active_time.total

Temporality: Claude Code can export delta or cumulative sums
(OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE). Delta is strongly
recommended in our client setup, but the ingest handler also converts
cumulative series to deltas using per-session state, so misconfigured
clients don't double-count.
"""

AGGREGATION_TEMPORALITY_DELTA = 1
AGGREGATION_TEMPORALITY_CUMULATIVE = 2

TRACKED_METRICS = {
    "claude_code.token.usage",
    "claude_code.cost.usage",
    "claude_code.session.count",
    "claude_code.lines_of_code.count",
    "claude_code.commit.count",
    "claude_code.pull_request.count",
    "claude_code.active_time.total",
}

# Resource attributes we carry onto every datapoint.
RESOURCE_KEYS = (
    "user.email",
    "user.id",
    "user.account_uuid",
    "organization.id",
    "session.id",
    "app.version",
    # common custom attribution keys set via OTEL_RESOURCE_ATTRIBUTES
    "team.id",
    "team",
    "department",
    "cost_center",
    "repo",
)


def _attr_value(v):
    """Unwrap an OTLP AnyValue. asInt is string-encoded in OTLP JSON."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    return None


def _attrs_to_dict(attr_list):
    out = {}
    for item in attr_list or []:
        key = item.get("key")
        if key is not None:
            out[key] = _attr_value(item.get("value"))
    return out


def _point_value(point):
    """Numeric value of a NumberDataPoint. Per the OTLP JSON encoding,
    asInt arrives as a decimal string; tolerate a plain number too."""
    if "asDouble" in point:
        return float(point["asDouble"])
    if "asInt" in point:
        return float(int(point["asInt"]))
    return 0.0


def parse_export_request(payload):
    """Flatten an ExportMetricsServiceRequest into datapoint dicts:

    {
      "metric": "claude_code.token.usage",
      "value": 1234.0,
      "temporality": 1 | 2,
      "time_unix_nano": 1730000000000000000,
      "start_time_unix_nano": ...,
      "attributes": {...datapoint attributes...},
      "resource": {...selected resource attributes...},
    }
    """
    points = []
    for rm in payload.get("resourceMetrics", []):
        resource_attrs = _attrs_to_dict(
            (rm.get("resource") or {}).get("attributes"))
        resource = {k: resource_attrs[k] for k in RESOURCE_KEYS
                    if k in resource_attrs}
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                name = metric.get("name")
                if name not in TRACKED_METRICS:
                    continue
                sum_block = metric.get("sum") or metric.get("gauge") or {}
                temporality = sum_block.get(
                    "aggregationTemporality", AGGREGATION_TEMPORALITY_DELTA)
                for point in sum_block.get("dataPoints", []):
                    points.append({
                        "metric": name,
                        "value": _point_value(point),
                        "temporality": temporality,
                        "time_unix_nano": int(point.get("timeUnixNano", 0)),
                        "start_time_unix_nano": int(
                            point.get("startTimeUnixNano", 0)),
                        "attributes": _attrs_to_dict(point.get("attributes")),
                        "resource": resource,
                    })
    return points


def series_key(point):
    """Stable identity for a cumulative series: session + metric + the
    attributes that define the series. Used to diff cumulative sums."""
    attrs = point["attributes"]
    ident = [
        point["resource"].get("session.id", "unknown"),
        point["metric"],
    ]
    for k in sorted(attrs):
        ident.append(f"{k}={attrs[k]}")
    return "|".join(ident)
