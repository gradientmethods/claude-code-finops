"""Turn normalized OTLP datapoints into daily rollup increments.

DynamoDB layout (single table, on-demand):

  PK             SK                    -> counters
  D#2026-07-06   TOTAL
  D#2026-07-06   U#alice@example.com   (per user)
  D#2026-07-06   M#claude-sonnet-5     (per model)
  D#2026-07-06   T#platform            (per team)

Counter fields:
  in_tokens, out_tokens, cache_read_tokens, cache_write_tokens,
  cost_usd (reported by Claude Code), est_cost_usd (from pricing table),
  cache_savings_usd, sessions, loc_added, loc_removed, commits, prs,
  active_seconds

Cumulative series state lives under PK=SERIES#<key>, SK=STATE with a TTL
so abandoned sessions age out.
"""

from datetime import datetime, timezone

from . import pricing
from .otlp import AGGREGATION_TEMPORALITY_CUMULATIVE

UNKNOWN_USER = "unknown"
UNATTRIBUTED_TEAM = "unattributed"

TEAM_KEYS = ("team.id", "team", "department", "cost_center")

TOKEN_FIELDS = {
    "input": "in_tokens",
    "output": "out_tokens",
    "cacheRead": "cache_read_tokens",
    "cacheCreation": "cache_write_tokens",
}

ALL_FIELDS = [
    "in_tokens", "out_tokens", "cache_read_tokens", "cache_write_tokens",
    "cost_usd", "est_cost_usd", "cache_savings_usd", "sessions",
    "loc_added", "loc_removed", "commits", "prs", "active_seconds",
]


def normalize_model(model):
    """Reduce a Bedrock model ID (us.anthropic.claude-sonnet-5-20250929-v1:0)
    or plain ID to a short display name."""
    if not model:
        return "unknown"
    m = str(model).lower()
    for prefix in ("us.", "eu.", "apac.", "global."):
        if m.startswith(prefix):
            m = m[len(prefix):]
    if m.startswith("anthropic."):
        m = m[len("anthropic."):]
    # strip -YYYYMMDD-vN:M suffix
    for sep in ("-v1:", "-v2:", "-v3:"):
        if sep in m:
            m = m.split(sep)[0]
            parts = m.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
                m = parts[0]
    return m


def point_date(point):
    ns = point.get("time_unix_nano") or 0
    if ns <= 0:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")


def identity(point):
    res = point.get("resource", {})
    user = res.get("user.email") or res.get("user.id") or UNKNOWN_USER
    team = UNATTRIBUTED_TEAM
    for key in TEAM_KEYS:
        if res.get(key):
            team = str(res[key])
            break
    model = normalize_model(point.get("attributes", {}).get("model"))
    return user, team, model


def field_increments(point, pricing_table=None):
    """Counter increments contributed by one (already delta) datapoint."""
    metric = point["metric"]
    value = point["value"]
    attrs = point.get("attributes", {})
    inc = {}
    if metric == "claude_code.token.usage":
        field = TOKEN_FIELDS.get(attrs.get("type"))
        if field is None or value <= 0:
            return inc
        inc[field] = value
        model = attrs.get("model")
        kwargs = {
            "in_tokens": {"input_tokens": value},
            "out_tokens": {"output_tokens": value},
            "cache_read_tokens": {"cache_read_tokens": value},
            "cache_write_tokens": {"cache_write_tokens": value},
        }[field]
        inc["est_cost_usd"] = pricing.estimate_cost_usd(
            model, pricing=pricing_table, **kwargs)
        if field == "cache_read_tokens":
            inc["cache_savings_usd"] = pricing.cache_savings_usd(
                model, value, pricing=pricing_table)
    elif metric == "claude_code.cost.usage":
        if value > 0:
            inc["cost_usd"] = value
    elif metric == "claude_code.session.count":
        if value > 0:
            inc["sessions"] = value
    elif metric == "claude_code.lines_of_code.count":
        field = {"added": "loc_added", "removed": "loc_removed"}.get(
            attrs.get("type"))
        if field and value > 0:
            inc[field] = value
    elif metric == "claude_code.commit.count":
        if value > 0:
            inc["commits"] = value
    elif metric == "claude_code.pull_request.count":
        if value > 0:
            inc["prs"] = value
    elif metric == "claude_code.active_time.total":
        if value > 0:
            inc["active_seconds"] = value
    return inc


def points_to_increments(points, resolve_delta=None, pricing_table=None):
    """Aggregate datapoints into {(pk, sk): {field: increment}}.

    resolve_delta(point) -> float converts a cumulative point's value to
    a delta using stored per-series state. Cumulative points are skipped
    entirely when no resolver is provided -- adding raw cumulative values
    would double-count, so dropping them is the safe failure mode.
    """
    buckets = {}
    for point in points:
        value = point["value"]
        if point.get("temporality") == AGGREGATION_TEMPORALITY_CUMULATIVE:
            if resolve_delta is None:
                continue
            value = resolve_delta(point)
            if value <= 0:
                continue
        adjusted = dict(point)
        adjusted["value"] = value
        inc = field_increments(adjusted, pricing_table)
        if not inc:
            continue
        date = point_date(point)
        user, team, model = identity(point)
        pk = f"D#{date}"
        for sk in ("TOTAL", f"U#{user}", f"M#{model}", f"T#{team}"):
            bucket = buckets.setdefault((pk, sk), {})
            for field, delta in inc.items():
                bucket[field] = bucket.get(field, 0) + delta
    return buckets
