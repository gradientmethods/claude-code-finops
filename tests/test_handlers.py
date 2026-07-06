"""Handler-level tests using fake AWS clients (no network, no moto)."""

import base64
import gzip
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("INGEST_TOKEN", "test-ingest-token-0123456789")
os.environ.setdefault("DASHBOARD_TOKEN", "test-dash-token-0123456789")

from ingest import app as ingest_app  # noqa: E402
from api import app as api_app  # noqa: E402
from alerts import app as alerts_app  # noqa: E402
from tests.test_otlp import sample_payload  # noqa: E402


class FakeTable:
    """Minimal DynamoDB Table stand-in for update_item/query/get_item."""

    def __init__(self):
        self.items = {}

    def update_item(self, Key=None, UpdateExpression=None,
                    ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ReturnValues=None):
        k = (Key["pk"], Key["sk"])
        item = self.items.setdefault(k, dict(Key))
        old = dict(item)
        touched = set()
        expr = UpdateExpression.strip()
        if expr.startswith("ADD "):
            for pair in expr[4:].split(","):
                name_ph, val_ph = pair.strip().split(" ")
                field = ExpressionAttributeNames[name_ph]
                item[field] = item.get(field, Decimal(0)) + \
                    ExpressionAttributeValues[val_ph]
                touched.add(field)
        elif expr.startswith("SET "):
            for pair in expr[4:].split(","):
                field, val_ph = [s.strip() for s in pair.strip().split("=")]
                item[field] = ExpressionAttributeValues[val_ph]
                touched.add(field)
        if ReturnValues == "UPDATED_OLD":
            # DynamoDB returns prior values of ALL touched attributes,
            # whether or not the new value differs
            return {"Attributes": {f: old[f] for f in touched if f in old}}
        return {}

    def query(self, KeyConditionExpression=None):
        # boto3 Key("pk").eq(v) -> extract value via private repr; instead we
        # only ever call with Key("pk").eq so match on stored pk values.
        values = KeyConditionExpression._values  # (Key('pk'), 'D#...')
        pk = values[1]
        return {"Items": [dict(v) for (p, _), v in self.items.items()
                          if p == pk]}

    def get_item(self, Key=None):
        k = (Key["pk"], Key["sk"])
        return {"Item": self.items[k]} if k in self.items else {}

    def put_item(self, Item=None):
        self.items[(Item["pk"], Item["sk"])] = dict(Item)


def make_event(payload, token="test-ingest-token-0123456789",
               path="/v1/metrics", gzipped=False):
    body = json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    if gzipped:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    return {
        "rawPath": path,
        "headers": headers,
        "isBase64Encoded": True,
        "body": base64.b64encode(body).decode(),
    }


def test_ingest_rejects_bad_token(monkeypatch):
    event = make_event(sample_payload(), token="wrong")
    resp = ingest_app.handler(event, None)
    assert resp["statusCode"] == 401


def test_ingest_rejects_garbage(monkeypatch):
    event = make_event(sample_payload())
    event["body"] = base64.b64encode(b"not json").decode()
    resp = ingest_app.handler(event, None)
    assert resp["statusCode"] == 400


def test_ingest_writes_rollups(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(ingest_app, "_table", lambda: fake)
    monkeypatch.setattr(ingest_app, "archive_raw", lambda *a, **k: None)
    resp = ingest_app.handler(make_event(sample_payload(), gzipped=True), None)
    assert resp["statusCode"] == 200
    totals = [v for (pk, sk), v in fake.items.items() if sk == "TOTAL"]
    assert len(totals) == 1
    assert totals[0]["in_tokens"] == Decimal("1500")
    assert totals[0]["cache_read_tokens"] == Decimal("90000")
    assert float(totals[0]["cost_usd"]) == 0.42
    # per-user, per-model, per-team rows exist
    sks = {sk for (_, sk) in fake.items.keys()}
    assert "U#alice@example.com" in sks
    assert "M#claude-sonnet-5" in sks
    assert "T#platform" in sks


def test_ingest_cumulative_deduped(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(ingest_app, "_table", lambda: fake)
    monkeypatch.setattr(ingest_app, "archive_raw", lambda *a, **k: None)
    # same cumulative payload delivered twice: second must add zero
    ingest_app.handler(make_event(sample_payload(temporality=2)), None)
    ingest_app.handler(make_event(sample_payload(temporality=2)), None)
    totals = [v for (pk, sk), v in fake.items.items() if sk == "TOTAL"][0]
    assert totals["in_tokens"] == Decimal("1500")


def test_logs_endpoint_accepts(monkeypatch):
    monkeypatch.setattr(ingest_app, "archive_raw", lambda *a, **k: None)
    resp = ingest_app.handler(
        make_event({"resourceLogs": []}, path="/v1/logs"), None)
    assert resp["statusCode"] == 200


def test_api_requires_token():
    resp = api_app.handler({"rawPath": "/api/summary", "headers": {}}, None)
    assert resp["statusCode"] == 401


def test_api_serves_dashboard_html():
    resp = api_app.handler({"rawPath": "/", "headers": {}}, None)
    assert resp["statusCode"] == 200
    assert "Claude Code FinOps" in resp["body"]


def test_api_blocks_path_traversal():
    resp = api_app.handler({"rawPath": "/../app.py", "headers": {}}, None)
    assert resp["statusCode"] == 200
    assert "<html" in resp["body"].lower()  # falls back to index, not source


def test_api_summary_aggregates(monkeypatch):
    def fake_fetch(days):
        item = {"cost_usd": Decimal("2.5"), "est_cost_usd": Decimal("2.4"),
                "in_tokens": Decimal(1000), "out_tokens": Decimal(200),
                "cache_read_tokens": Decimal(5000),
                "cache_savings_usd": Decimal("0.9")}
        return {"2026-07-05": {
            "TOTAL": item,
            "users": {"alice@example.com": item},
            "models": {"claude-sonnet-5": item},
            "teams": {"platform": item},
        }}
    monkeypatch.setattr(api_app, "fetch_days", fake_fetch)
    event = {"rawPath": "/api/summary",
             "headers": {"authorization": "Bearer test-dash-token-0123456789"},
             "queryStringParameters": {"days": "7"}}
    resp = api_app.handler(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["totals"]["cost_usd"] == 2.5
    assert body["active_users"] == 1
    assert body["top_users"][0]["user"] == "alice@example.com"
    assert body["series"][0]["by_model"]["claude-sonnet-5"] == 2.5


def test_alerts_spike_detection(monkeypatch):
    from datetime import date, timedelta
    today = date(2026, 7, 6)
    spend = {today.isoformat(): 120.0}
    for i in range(1, 8):
        spend[(today - timedelta(days=i)).isoformat()] = 10.0

    def fake_day_items(d):
        return [{"sk": "U#alice@example.com",
                 "cost_usd": Decimal(str(spend.get(d, 0.0)))}]

    monkeypatch.setattr(alerts_app, "_day_items", fake_day_items)
    alerts = alerts_app.check_spikes(today)
    assert len(alerts) == 1
    assert "alice@example.com" in alerts[0]["subject"]


def test_alerts_no_spike_below_min(monkeypatch):
    from datetime import date

    def fake_day_items(d):
        return [{"sk": "U#bob@example.com", "cost_usd": Decimal("5")}]

    monkeypatch.setattr(alerts_app, "_day_items", fake_day_items)
    assert alerts_app.check_spikes(date(2026, 7, 6)) == []


def test_alerts_budget(monkeypatch):
    from datetime import date

    def fake_day_items(d):
        return [{"sk": "TOTAL", "cost_usd": Decimal("300")},
                {"sk": "T#platform", "cost_usd": Decimal("200")}]

    monkeypatch.setattr(alerts_app, "_day_items", fake_day_items)
    monkeypatch.setattr(alerts_app, "MONTHLY_BUDGET_USD", 1000.0)
    monkeypatch.setattr(alerts_app, "BUDGETS_JSON", '{"platform": 500}')
    alerts = alerts_app.check_budgets(date(2026, 7, 6))  # 6 days x 300 = 1800
    assert any("monthly budget" in a["subject"] for a in alerts)
    assert any("platform" in a["subject"] for a in alerts)
