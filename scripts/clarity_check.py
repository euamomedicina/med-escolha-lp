#!/usr/bin/env python3
"""
Clarity milestone checker.

Hits the Microsoft Clarity Data Export API ONCE per run with num_days=3 and
tracks the peak 3-day window seen so far. Fires when that peak crosses a new
1000-session multiple.

Why 1 call per run: the Clarity Data Export API has a hard quota of ~10
requests per day per project. Doing 3 calls per run (to infer daily breakdown)
burns through the quota fast and trips 429 rate-limit errors that silently
make the script report zero. One call per run leaves plenty of headroom for
manual debug runs.

What we lose: per-day breakdown. If you need that, open the dashboard
directly (it's where Clarity natively exposes daily timeseries).

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
  - "rate_limited" Clarity returned 429. Workflow should retry next run.
  - "no_milestone" Traffic exists but peak 3-day window didn't cross a new 1000-multiple.
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
            "peak_sessions_3d": 0,
            "last_seen_sessions_3d": 0,
            "last_milestone": 0,
            "last_check": None,
            "last_report": None,
            "history": [],  # rolling list of {date, sessions_3d}
        }
    state = json.loads(STATE_FILE.read_text())
    # Migrate older state shapes silently.
    state.setdefault("peak_sessions_3d", state.get("cumulative_sessions", 0))
    state.setdefault("last_seen_sessions_3d", 0)
    state.setdefault("last_milestone", 0)
    state.setdefault("last_check", None)
    state.setdefault("last_report", None)
    state.setdefault("history", [])
    # Drop legacy fields we no longer maintain.
    state.pop("cumulative_sessions", None)
    state.pop("daily_counts", None)
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
    status = "no_data"
    msg = "Clarity returned no data (likely zero traffic)."
    if code == 429:
        status = "rate_limited"
        msg = "Clarity rate-limited (429). Will retry next run."
    elif isinstance(payload, dict) and payload.get("_empty"):
        msg = "Empty response from Clarity."
    result = {
        "status": status,
        "http_status": code,
        "peak_sessions_3d": state["peak_sessions_3d"],
        "last_seen_sessions_3d": state["last_seen_sessions_3d"],
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


def write_report_skeleton(milestone, previous, sessions_3d, history, insights, output_date):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"clarity-{output_date}.md"
    history_lines = "\n".join(
        f"- **{entry['date']}:** {entry['sessions_3d']} sessões (janela 3 dias)"
        for entry in history[-14:]
    )
    body = f"""# Relatório Clarity, {output_date}

**Milestone atingido:** {milestone} sessões em janela de 3 dias (anterior: {previous}).
**Pico atual (3-day window):** {sessions_3d} sessões.

## Histórico das checagens diárias (últimas 14)

{history_lines or '- (sem histórico ainda)'}

> Cada linha mostra o total da janela móvel de 3 dias no dia da checagem.
> Pra ver breakdown por dia, abra o dashboard do Clarity diretamente:
> https://clarity.microsoft.com

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

    # ONE call per run. num_days=3 to capture as much of a window as the API allows.
    # Dimension=URL so we can filter by domain (skip previews/staging).
    payload = fetch_clarity(token, num_days=3, dimensions=["URL"])

    if isinstance(payload, dict):
        if payload.get("_http_error") in (500, 404, 429) or payload.get("_empty"):
            _no_data_response(payload, state, now_iso)
            return
        if "_url_error" in payload or "_error" in payload:
            _error_response(payload, state, now_iso)
            return

    sessions_3d, sessions_all_urls, matched_urls, skipped_urls = extract_session_count(
        payload, url_filter=url_filter
    )

    peak = max(state["peak_sessions_3d"], sessions_3d)
    previous_milestone = state["last_milestone"]
    current_milestone = (peak // MILESTONE_STEP) * MILESTONE_STEP
    crossed = current_milestone > previous_milestone and current_milestone >= MILESTONE_STEP

    # Append to history (keep last 60 entries to bound state file size).
    state["history"].append({"date": today, "sessions_3d": sessions_3d})
    state["history"] = state["history"][-60:]

    result = {
        "sessions_3d_window": sessions_3d,
        "sessions_all_urls_3d": sessions_all_urls,
        "peak_sessions_3d": peak,
        "previous_peak": state["peak_sessions_3d"],
        "url_filter": url_filter,
        "matched_urls": matched_urls,
        "skipped_urls": skipped_urls,
        "milestone": current_milestone,
        "previous_milestone": previous_milestone,
        "last_check": now_iso,
        "date": today,
    }

    if crossed:
        # No extra Clarity call here. Re-use the same payload we already have
        # so we don't burn another request from the daily quota.
        result["status"] = "milestone_hit"
        result["raw_insights"] = payload
        report_path = write_report_skeleton(
            milestone=current_milestone,
            previous=previous_milestone,
            sessions_3d=sessions_3d,
            history=state["history"],
            insights=payload,
            output_date=today,
        )
        result["report_path"] = str(report_path.relative_to(PROJECT_ROOT))
        state["last_milestone"] = current_milestone
        state["last_report"] = today
    else:
        result["status"] = "no_milestone"

    state["peak_sessions_3d"] = peak
    state["last_seen_sessions_3d"] = sessions_3d
    state["last_check"] = now_iso
    save_state(state)
    write_output(result)

    print(json.dumps({k: v for k, v in result.items() if k != "raw_insights"}))


if __name__ == "__main__":
    main()
