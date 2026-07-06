import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared import otlp  # noqa: E402


def sample_payload(temporality=1):
    """A realistic Claude Code OTLP http/json export."""
    return {
        "resourceMetrics": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "claude-code"}},
                {"key": "user.email", "value": {"stringValue": "alice@example.com"}},
                {"key": "session.id", "value": {"stringValue": "sess-123"}},
                {"key": "organization.id", "value": {"stringValue": "org-1"}},
                {"key": "team.id", "value": {"stringValue": "platform"}},
            ]},
            "scopeMetrics": [{
                "scope": {"name": "com.anthropic.claude_code"},
                "metrics": [
                    {
                        "name": "claude_code.token.usage",
                        "sum": {
                            "aggregationTemporality": temporality,
                            "isMonotonic": True,
                            "dataPoints": [
                                {
                                    "attributes": [
                                        {"key": "type", "value": {"stringValue": "input"}},
                                        {"key": "model", "value": {"stringValue": "claude-sonnet-5"}},
                                    ],
                                    "startTimeUnixNano": "1751500000000000000",
                                    "timeUnixNano": "1751500060000000000",
                                    "asInt": "1500",
                                },
                                {
                                    "attributes": [
                                        {"key": "type", "value": {"stringValue": "cacheRead"}},
                                        {"key": "model", "value": {"stringValue": "claude-sonnet-5"}},
                                    ],
                                    "timeUnixNano": "1751500060000000000",
                                    "asInt": "90000",
                                },
                            ],
                        },
                    },
                    {
                        "name": "claude_code.cost.usage",
                        "sum": {
                            "aggregationTemporality": temporality,
                            "isMonotonic": True,
                            "dataPoints": [{
                                "attributes": [
                                    {"key": "model", "value": {"stringValue": "claude-sonnet-5"}},
                                ],
                                "timeUnixNano": "1751500060000000000",
                                "asDouble": 0.42,
                            }],
                        },
                    },
                    {
                        "name": "claude_code.some_future_metric",
                        "sum": {"dataPoints": [{"asInt": "1"}]},
                    },
                ],
            }],
        }]
    }


def test_parse_flattens_datapoints():
    points = otlp.parse_export_request(sample_payload())
    assert len(points) == 3  # unknown metric ignored
    token_points = [p for p in points if p["metric"] == "claude_code.token.usage"]
    assert len(token_points) == 2
    p = token_points[0]
    assert p["value"] == 1500.0
    assert p["attributes"]["type"] == "input"
    assert p["attributes"]["model"] == "claude-sonnet-5"
    assert p["resource"]["user.email"] == "alice@example.com"
    assert p["resource"]["team.id"] == "platform"
    assert p["temporality"] == 1


def test_as_int_is_string_encoded():
    # OTLP JSON encodes int64 as strings; both must parse
    payload = sample_payload()
    dp = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"]["dataPoints"][0]
    dp["asInt"] = 2500  # plain number should also be tolerated
    points = otlp.parse_export_request(payload)
    assert points[0]["value"] == 2500.0


def test_cost_metric_as_double():
    points = otlp.parse_export_request(sample_payload())
    cost = [p for p in points if p["metric"] == "claude_code.cost.usage"][0]
    assert cost["value"] == 0.42


def test_series_key_stable_and_distinct():
    points = otlp.parse_export_request(sample_payload())
    keys = [otlp.series_key(p) for p in points]
    assert len(set(keys)) == 3
    assert keys == [otlp.series_key(p) for p in
                    otlp.parse_export_request(sample_payload())]
    assert "sess-123" in keys[0]


def test_empty_payload():
    assert otlp.parse_export_request({}) == []
    assert otlp.parse_export_request({"resourceMetrics": []}) == []


def test_round_trip_through_json():
    raw = json.dumps(sample_payload())
    points = otlp.parse_export_request(json.loads(raw))
    assert len(points) == 3
