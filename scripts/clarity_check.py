#!/usr/bin/env python3
"""
Clarity milestone checker.

Hits the Microsoft Clarity Data Export API to track cumulative sessions for the
target landing page and flags when a 1000-session milestone has been crossed.

The Clarity API only exposes a 1-3 day rolling window with no native per-day
breakdown. We fake the breakdown by querying num_days=1, 2, and 3 and
subtracting the totals (yesterday = sum_1d, day-2 = sum_2d - sum_1d,
day-3 = sum_3d - sum_2d). Daily counts are persisted to .clarity-state.json so
the cumulative total survives across runs and recovers from up to 2 days of
missed workflow runs.

URL filtering: only sessions whose URL contains DEFAULT_URL_FILTER (override
via CLARITY_URL_FILTER env var) are counted, so previews/staging hits don't
inflate the count.

Token loading:
  1. env CLARITY_API_TOKEN (preferred, used in CI)
  2. file ~/.config/clarity/api_token (local dev fallback)

Outputs:
  - stdout: compact JSON summary (workflow parses .status from this)
  - <root>/clarity-output.json: full JSON payload (used by next workflow step)
  - <root>/reports/clarity-YYYY-MM-DD.md: skeleton report (only on milestone_hit)

Status values:
  - "no_data"      Clarity returned 5xx/empty (no traffic yet). Silent exit.
  - "no_milestone" Traffic exists but didn't cross a new 1000-multiple.
  - "milestone_hit" Crossed milestone. Workflow creates issue + commits report.
  - "error"        Unexpected error.
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / ".clarity-state.json"
OUTPUT_FILE = PROJECT_ROOT / "clarity-output.json"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOCAL_TOKEN_FILE = Path.home() / ".config" / "clarity" / "api_token"
API_BASE = "https://www.clarity.ms/export-data/api/v1/project-live-insights"

MILESTONE_STEP = 1000
DEFAULT_URL_FILTER = "match.medescolha.com"


def load_token() -> str:
    env_token = os.environ.get("CLARITY_API_TOKEN", "").strip()
    if env_token:
        return env_token
    if LOCAL_TOKEN_FILE.exists():
        return LOCAL_TOKEN_FILE.read_text().strip()
    raise RuntimeError(
        "CLARITY_API_TOKEN env var not set and no local token file at "
        f"{LOCAL_TOKEN_FILE}"
    )


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "daily_counts": {},
            "cumulative_sessions": 0,
            "last_milestone": 0,
            "last_check": None,
            "last_report": None,
        }
    state = json.loads(STATE_FILE.read_text())
    state.setdefault("daily_counts", {})
    state.setdefault("cumulative_sessions", 0)
    state.setdefault("last_milestone", 0)
    state.setdefault("last_check", None)
    state.setdefault("last_report", None)
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def write_output(payload: dict) -> None:
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2))


def fetch_clarity(token: str, num_days: int = 1, dimensions=None):
    params = {"numOfDays": str(num_days)}
    if dimensions:
        for i, d in enumerate(dimensions[:3], start=1):
            params[f"dimension{i}"] = d
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            if not body.strip():
                return {"_empty": True}
            return json.loads(body)
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_message": str(e)}
    except urllib.error.URLError as e:
        return {"_url_error": str(e)}
    except Exception as e:
        return {"_error": str(e)}


def _row_url_fields(row: dict) -> list:
    return [v for k, v in row.items() if isinstance(v, str) and k.lower() in ("url", "pageurl", "page_url")]


def extract_session_count(payload, url_filter: str = None):
    """Return (filtered_total, all_total, matched_urls, skipped_urls)."""
    if not isinstance(payload, list):
        return 0, 0, [], []

    filtered_total = 0
    all_total = 0
    matched, skipped = set(), set()

    for metric in payload:
        if not isinstance(metric, dict):
            continue
        if metric.get("metricName") not in ("Traffic", "TrafficMetrics"):
            continue
        for row in metric.get("information", []) or []:
            if not isinstance(row, dict):
                continue
            count = 0
            for key in ("sessionsCount", "totalSessionCount", "numOfSessions", "sessions"):
                if key in row:
                    try:
                        count = int(row[key])
                    except (ValueError, TypeError):
                        count = 0
                    break
            all_total += count
            urls = _row_url_fields(row)
            if url_filter is None or not urls:
                filtered_total += count
                continue
            if any(url_filter in u for u in urls):
                filtered_total += count
                for u in urls:
                    if url_filter in u:
                        matched.add(u)
            else:
                for u in urls:
                    skipped.add(u)

    return filtered_total, all_total, sorted(matched), sorted(skipped)


def _no_data_response(payload, state, now_iso):
    """Build a no_data result and persist state."""
    code = payload.get("_http_error") if isinstance(payload, dict) else None
    msg = "Clarity returned no data (likely zero traffic)."
    if isinstance(payload, dict) and payload.get("_empty"):
        msg = "Empty response from Clarity."
    result = {
        "status": "no_data",
        "http_status": code,
        "current_sessions": state["cumulative_sessions"],
        "previous_sessions": state["cumulative_sessions"],
        "last_check": now_iso,
        "message": msg,
    }
    state["last_check"] = now_iso
    save_state(state)
    write_output(result)
    print(json.dumps(result))


def _error_response(err, state, now_iso):
    result = {"status": "error", "error": err, "last_check": now_iso}
    state["last_check"] = now_iso
    save_state(state)
    write_output(result)
    print(json.dumps(result))


def write_report_skeleton(milestone, previous, current, daily_counts, insights, output_date):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"clarity-{output_date}.md"
    daily_lines = "\n".join(
        f"- **{day}:** {count} sessões" for day, count in sorted(daily_counts.items(), reverse=True)[:14]
    )
    body = f"""# Relatório Clarity, {output_date}

**Milestone atingido:** {milestone} sessões acumuladas (anterior: {previous}).
**Total atual:** {current} sessões.

## Sessões por dia (últimos 14 dias rastreados)

{daily_lines or '- (sem histórico ainda)'}

## Dados Brutos (Clarity, últimos 3 dias)

```json
{json.dumps(insights, indent=2, ensure_ascii=False)[:8000]}
```

## Análise (gerar manualmente ou via Claude Code)

> Abra este arquivo no Claude Code e peça uma análise das métricas + recomendações de conversão pra `index.html`.
"""
    path.write_text(body)
    return path


def main():
    try:
        token = load_token()
    except RuntimeError as e:
        write_output({"status": "error", "error": str(e)})
        print(json.dumps({"status": "error", "error": str(e)}))
        return

    state = load_state()
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    today = now_utc.strftime("%Y-%m-%d")
    url_filter = os.environ.get("CLARITY_URL_FILTER", DEFAULT_URL_FILTER).strip() or None

    # Fetch 3 windows. URL dimension so we can filter by domain.
    payloads = {}
    fetch_meta = {}
    for n in (1, 2, 3):
        p = fetch_clarity(token, num_days=n, dimensions=["URL"])
        payloads[n] = p
        fetch_meta[n] = {"type": type(p).__name__}

    # If all three returned a Clarity 5xx or empty, treat as no_data.
    def is_no_data(p):
        if isinstance(p, dict) and (p.get("_http_error") in (500, 404) or p.get("_empty")):
            return True
        return False

    if all(is_no_data(payloads[n]) for n in (1, 2, 3)):
        _no_data_response(payloads[1], state, now_iso)
        return

    # If any payload is an outright error (network, parse), surface it.
    for n in (1, 2, 3):
        p = payloads[n]
        if isinstance(p, dict) and ("_url_error" in p or "_error" in p):
            _error_response(p, state, now_iso)
            return

    # Sum sessions per window, filtered by URL.
    sums = {}
    matched_urls = set()
    skipped_urls = set()
    for n in (1, 2, 3):
        p = payloads[n]
        if isinstance(p, list):
            total, _all, m, s = extract_session_count(p, url_filter=url_filter)
            sums[n] = total
            matched_urls.update(m)
            skipped_urls.update(s)
        else:
            sums[n] = 0

    # Derive per-day counts by subtraction. Map to actual dates.
    # num_days=1 = sessions in the last 24h ending now. We treat this as "yesterday" UTC.
    # num_days=2 = last 48h. day-2 contribution = sums[2] - sums[1]
    # num_days=3 = last 72h. day-3 contribution = sums[3] - sums[2]
    day_minus_1 = sums.get(1, 0)
    day_minus_2 = max(0, sums.get(2, 0) - sums.get(1, 0))
    day_minus_3 = max(0, sums.get(3, 0) - sums.get(2, 0))

    yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
    two_days_ago = (now_utc - timedelta(days=2)).strftime("%Y-%m-%d")
    three_days_ago = (now_utc - timedelta(days=3)).strftime("%Y-%m-%d")

    # Update daily_counts: overwrite the 3 days we just observed.
    state["daily_counts"][yesterday] = day_minus_1
    state["daily_counts"][two_days_ago] = day_minus_2
    state["daily_counts"][three_days_ago] = day_minus_3

    # Cumulative = sum of all daily counts ever recorded.
    new_cumulative = sum(state["daily_counts"].values())
    previous_milestone = state["last_milestone"]
    current_milestone = (new_cumulative // MILESTONE_STEP) * MILESTONE_STEP
    crossed = current_milestone > previous_milestone and current_milestone >= MILESTONE_STEP

    result = {
        "current_sessions": new_cumulative,
        "previous_sessions": state["cumulative_sessions"],
        "url_filter": url_filter,
        "matched_urls": sorted(matched_urls),
        "skipped_urls": sorted(skipped_urls),
        "daily_breakdown": {
            yesterday: day_minus_1,
            two_days_ago: day_minus_2,
            three_days_ago: day_minus_3,
        },
        "window_totals": {
            "last_1_day": sums.get(1, 0),
            "last_2_days": sums.get(2, 0),
            "last_3_days": sums.get(3, 0),
        },
        "milestone": current_milestone,
        "previous_milestone": previous_milestone,
        "last_check": now_iso,
        "date": today,
    }

    if crossed:
        detailed = fetch_clarity(token, num_days=3, dimensions=["URL", "Device", "Country"])
        result["status"] = "milestone_hit"
        result["raw_insights"] = detailed
        report_path = write_report_skeleton(
            milestone=current_milestone,
            previous=previous_milestone,
            current=new_cumulative,
            daily_counts=state["daily_counts"],
            insights=detailed,
            output_date=today,
        )
        result["report_path"] = str(report_path.relative_to(PROJECT_ROOT))
        state["last_milestone"] = current_milestone
        state["last_report"] = today
    else:
        result["status"] = "no_milestone"

    state["cumulative_sessions"] = new_cumulative
    state["last_check"] = now_iso
    save_state(state)
    write_output(result)

    # Compact stdout payload (without raw_insights which is huge).
    print(json.dumps({k: v for k, v in result.items() if k != "raw_insights"}))


if __name__ == "__main__":
    main()
