"""Dashboard API + static file server.

  GET /                      dashboard (static, no data inside)
  GET /api/summary?days=30   totals, daily series, model + team breakdowns
  GET /api/users?days=30     per-user table rows

Data endpoints require Authorization: Bearer <DASHBOARD_TOKEN> (the
dashboard prompts once and stores it in localStorage). Static assets are
served unauthenticated; they contain no data.
"""

import json
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.rollup import ALL_FIELDS  # noqa: E402

TABLE_NAME = os.environ.get("TABLE_NAME", "")
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
MAX_DAYS = 120

_table = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def _num(item, field):
    v = item.get(field, 0)
    return float(v) if isinstance(v, Decimal) else (v or 0)


def _zero():
    return {f: 0.0 for f in ALL_FIELDS}


def _accumulate(target, item):
    for f in ALL_FIELDS:
        target[f] = target.get(f, 0.0) + _num(item, f)


def _dates(days):
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat()
            for i in range(days - 1, -1, -1)]


def fetch_days(days):
    """{date: {"TOTAL": item, "users": {u: item}, "models": {...}, "teams": {...}}}"""
    out = {}
    for date in _dates(days):
        resp = table().query(KeyConditionExpression=Key("pk").eq(f"D#{date}"))
        day = {"TOTAL": None, "users": {}, "models": {}, "teams": {}}
        for item in resp.get("Items", []):
            sk = item.get("sk", "")
            if sk == "TOTAL":
                day["TOTAL"] = item
            elif sk.startswith("U#"):
                day["users"][sk[2:]] = item
            elif sk.startswith("M#"):
                day["models"][sk[2:]] = item
            elif sk.startswith("T#"):
                day["teams"][sk[2:]] = item
        out[date] = day
    return out


def summary(days):
    data = fetch_days(days)
    totals = _zero()
    by_model, by_team = {}, {}
    series = []
    for date, day in data.items():
        entry = {"date": date, "cost_usd": 0.0, "est_cost_usd": 0.0,
                 "by_model": {}}
        if day["TOTAL"]:
            _accumulate(totals, day["TOTAL"])
            entry["cost_usd"] = _num(day["TOTAL"], "cost_usd")
            entry["est_cost_usd"] = _num(day["TOTAL"], "est_cost_usd")
        for model, item in day["models"].items():
            _accumulate(by_model.setdefault(model, _zero()), item)
            entry["by_model"][model] = _num(item, "cost_usd") or _num(
                item, "est_cost_usd")
        for team, item in day["teams"].items():
            _accumulate(by_team.setdefault(team, _zero()), item)
        series.append(entry)

    active_users = set()
    user_totals = {}
    for day in data.values():
        for user, item in day["users"].items():
            active_users.add(user)
            _accumulate(user_totals.setdefault(user, _zero()), item)
    top_users = sorted(
        ({"user": u, **t} for u, t in user_totals.items()),
        key=lambda r: r["cost_usd"] or r["est_cost_usd"], reverse=True)[:10]

    return {
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": totals,
        "series": series,
        "by_model": by_model,
        "by_team": by_team,
        "top_users": top_users,
        "active_users": len(active_users),
    }


def users(days):
    data = fetch_days(days)
    user_totals = {}
    last_active = {}
    for date, day in data.items():
        for user, item in day["users"].items():
            _accumulate(user_totals.setdefault(user, _zero()), item)
            last_active[user] = max(last_active.get(user, ""), date)
    rows = [{"user": u, "last_active": last_active.get(u, ""), **t}
            for u, t in user_totals.items()]
    rows.sort(key=lambda r: r["cost_usd"] or r["est_cost_usd"], reverse=True)
    return {"days": days, "users": rows}


def _authorized(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    return bool(DASHBOARD_TOKEN) and auth == f"Bearer {DASHBOARD_TOKEN}"


def _json_response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=float),
    }


def _static_response(path):
    name = path.lstrip("/") or "index.html"
    # prevent traversal; only serve files that exist inside static/
    full = os.path.normpath(os.path.join(STATIC_DIR, name))
    if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
        full = os.path.join(STATIC_DIR, "index.html")
    ctype = mimetypes.guess_type(full)[0] or "text/html"
    with open(full, "rb") as f:
        body = f.read()
    return {
        "statusCode": 200,
        "headers": {"Content-Type": ctype,
                    "Cache-Control": "public, max-age=300"},
        "body": body.decode("utf-8"),
    }


def handler(event, context):
    path = (event.get("rawPath") or
            event.get("requestContext", {}).get("http", {}).get("path", "/"))
    params = event.get("queryStringParameters") or {}

    if path.startswith("/api/"):
        if not _authorized(event):
            return _json_response(401, {"error": "unauthorized"})
        try:
            days = max(1, min(MAX_DAYS, int(params.get("days", "30"))))
        except ValueError:
            days = 30
        if path == "/api/summary":
            return _json_response(200, summary(days))
        if path == "/api/users":
            return _json_response(200, users(days))
        return _json_response(404, {"error": "not found"})

    return _static_response(path)
