import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared import rollup  # noqa: E402
from shared.otlp import (  # noqa: E402
    AGGREGATION_TEMPORALITY_CUMULATIVE,
    AGGREGATION_TEMPORALITY_DELTA,
)

# 2026-07-03 00:01 UTC
NANOS = 1_782_086_460 * 1_000_000_000


def point(metric="claude_code.token.usage", value=1000.0,
          attrs=None, resource=None, temporality=AGGREGATION_TEMPORALITY_DELTA):
    return {
        "metric": metric,
        "value": value,
        "temporality": temporality,
        "time_unix_nano": NANOS,
        "start_time_unix_nano": NANOS,
        "attributes": attrs or {"type": "input", "model": "claude-sonnet-5"},
        "resource": resource or {"user.email": "alice@example.com",
                                 "session.id": "s1", "team.id": "platform"},
    }


def test_normalize_model():
    assert rollup.normalize_model(
        "us.anthropic.claude-sonnet-5-20250929-v1:0") == "claude-sonnet-5"
    assert rollup.normalize_model("claude-sonnet-5") == "claude-sonnet-5"
    assert rollup.normalize_model(
        "eu.anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5"
    assert rollup.normalize_model(None) == "unknown"


def test_increments_fan_out_to_all_scopes():
    buckets = rollup.points_to_increments([point()])
    date = rollup.point_date(point())
    pk = f"D#{date}"
    assert (pk, "TOTAL") in buckets
    assert (pk, "U#alice@example.com") in buckets
    assert (pk, "M#claude-sonnet-5") in buckets
    assert (pk, "T#platform") in buckets
    total = buckets[(pk, "TOTAL")]
    assert total["in_tokens"] == 1000.0
    # 1000 input tokens on sonnet at $3/MTok
    assert abs(total["est_cost_usd"] - 0.003) < 1e-9


def test_cache_read_generates_savings():
    p = point(attrs={"type": "cacheRead", "model": "claude-sonnet-5"},
              value=1_000_000)
    buckets = rollup.points_to_increments([p])
    total = buckets[(f"D#{rollup.point_date(p)}", "TOTAL")]
    assert total["cache_read_tokens"] == 1_000_000
    # cacheRead costs 10% of $3 -> $0.30; savings = $2.70
    assert abs(total["est_cost_usd"] - 0.30) < 1e-6
    assert abs(total["cache_savings_usd"] - 2.70) < 1e-6


def test_cost_and_activity_metrics():
    points = [
        point("claude_code.cost.usage", 0.5, attrs={"model": "claude-sonnet-5"}),
        point("claude_code.session.count", 1, attrs={}),
        point("claude_code.lines_of_code.count", 120, attrs={"type": "added"}),
        point("claude_code.lines_of_code.count", 30, attrs={"type": "removed"}),
        point("claude_code.commit.count", 2, attrs={}),
        point("claude_code.pull_request.count", 1, attrs={}),
        point("claude_code.active_time.total", 55.5, attrs={"type": "cli"}),
    ]
    buckets = rollup.points_to_increments(points)
    total = buckets[(f"D#{rollup.point_date(points[0])}", "TOTAL")]
    assert total["cost_usd"] == 0.5
    assert total["sessions"] == 1
    assert total["loc_added"] == 120
    assert total["loc_removed"] == 30
    assert total["commits"] == 2
    assert total["prs"] == 1
    assert total["active_seconds"] == 55.5


def test_unattributed_identity_defaults():
    p = point(resource={"session.id": "s1"})
    buckets = rollup.points_to_increments([p])
    pk = f"D#{rollup.point_date(p)}"
    assert (pk, "U#unknown") in buckets
    assert (pk, "T#unattributed") in buckets


def test_cumulative_skipped_without_resolver():
    p = point(temporality=AGGREGATION_TEMPORALITY_CUMULATIVE)
    assert rollup.points_to_increments([p], resolve_delta=None) == {}


def test_cumulative_resolved_to_delta():
    seen = {}

    def resolver(pt):
        key = pt["metric"]
        prev = seen.get(key)
        seen[key] = pt["value"]
        return pt["value"] if prev is None else pt["value"] - prev

    p1 = point(value=1000, temporality=AGGREGATION_TEMPORALITY_CUMULATIVE)
    p2 = point(value=1600, temporality=AGGREGATION_TEMPORALITY_CUMULATIVE)
    b1 = rollup.points_to_increments([p1], resolve_delta=resolver)
    b2 = rollup.points_to_increments([p2], resolve_delta=resolver)
    date = rollup.point_date(p1)
    assert b1[(f"D#{date}", "TOTAL")]["in_tokens"] == 1000
    assert b2[(f"D#{date}", "TOTAL")]["in_tokens"] == 600


def test_negative_and_zero_values_ignored():
    assert rollup.points_to_increments([point(value=0)]) == {}
    assert rollup.points_to_increments([point(value=-5)]) == {}
