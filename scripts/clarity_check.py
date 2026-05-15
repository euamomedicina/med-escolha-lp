#!/usr/bin/env python3
"""
Clarity milestone checker.

Hits Microsoft Clarity Data Export API, tracks total sessions, and signals
when a 1000-session milestone has been crossed since the last run.

Designed to run both locally and inside GitHub Actions.

Token loading order:
  1. env CLARITY_API_TOKEN (preferred, used in CI)
  2. file ~/.config/clarity/api_token (local dev fallback)

State file: <project_root>/.clarity-state.json (committed to repo so CI can persist)

Outputs:
  - stdout: compact JSON with status + summary (for shell parsing)
  - <project_root>/clarity-output.json: full JSON payload (for GH Actions to consume)
  - On milestone_hit: <project_root>/reports/clarity-YYYY-MM-DD.md (raw skeleton)

Status values:
  - "no_data"      Clarity returned 5xx/404/empty (no traffic yet). Silent exit.
  - "no_milestone" Sessions accumulated but didn't cross a new 1000-multiple. Silent exit.
  - "milestone_hit" Crossed milestone. Workflow will create issue + commit report.
  - "error"        Unexpected error.

Exit code is always 0 unless a hard error occurred (so workflow continues).
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / ".clarity-state.json"
OUTPUT_FILE = PROJECT_ROOT / "clarity-output.json"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOCAL_TOKEN_FILE = Path.home() / ".config" / "clarity" / "api_token"
API_BASE = "https://www.clarity.ms/export-data/api/v1/project-live-insights"

MILESTONE_STEP = 1000


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
            "cumulative_sessions": 0,
            "last_milestone": 0,
            "last_check": None,
            "last_report": None,
        }
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def write_output(payload: dict) -> None:
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2))


def fetch_clarity(token: str, num_days: int = 3, dimensions=None) -> dict:
    """Hit Clarity API. Returns parsed JSON dict/list, or special dict on error."""
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


def extract_session_count(payload) -> int:
    """Pull total sessions from Clarity payload.

    Clarity returns a list of metric objects. The 'Traffic' metric has session counts
    in its `information` rows. Field names vary; we try several.
    """
    if not isinstance(payload, list):
        return 0
    total = 0
    for metric in payload:
        if not isinstance(metric, dict):
            continue
        name = metric.get("metricName", "")
        if name in ("Traffic", "TrafficMetrics"):
            info = metric.get("information", []) or []
            for row in info:
                if not isinstance(row, dict):
                    continue
                for key in ("sessionsCount", "totalSessionCount", "numOfSessions", "sessions"):
                    if key in row:
                        try:
                            total += int(row[key])
                        except (ValueError, TypeError):
                            pass
                        break
    return total


def write_report_skeleton(milestone: int, previous: int, current: int, insights, output_date: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"clarity-{output_date}.md"
    body = f"""# Relatório Clarity, {output_date}

**Milestone atingido:** {milestone} sessões acumuladas (anterior: {previous}).
**Total atual:** {current} sessões.

## Dados Brutos (Clarity)

```json
{json.dumps(insights, indent=2, ensure_ascii=False)[:8000]}
```

## Análise (gerada manualmente ou via Claude Code)

> Abra este arquivo no Claude Code e peça uma análise das métricas + recomendações de conversão pra `index.html`.
"""
    path.write_text(body)
    return path


def main():
    try:
        token = load_token()
    except RuntimeError as e:
        result = {"status": "error", "error": str(e)}
        write_output(result)
        print(json.dumps(result))
        sys.exit(0)

    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Daily count (avoid double-counting from larger windows)
    one_day = fetch_clarity(token, num_days=1)

    # Error handling
    if isinstance(one_day, dict):
        code = one_day.get("_http_error")
        if code in (500, 404):
            result = {
                "status": "no_data",
                "http_status": code,
                "current_sessions": state["cumulative_sessions"],
                "previous_sessions": state["cumulative_sessions"],
                "last_check": now_iso,
                "message": "Clarity returned no data (likely zero traffic).",
            }
            state["last_check"] = now_iso
            save_state(state)
            write_output(result)
            print(json.dumps(result))
            return
        if one_day.get("_empty"):
            result = {
                "status": "no_data",
                "current_sessions": state["cumulative_sessions"],
                "previous_sessions": state["cumulative_sessions"],
                "last_check": now_iso,
                "message": "Empty response from Clarity.",
            }
            state["last_check"] = now_iso
            save_state(state)
            write_output(result)
            print(json.dumps(result))
            return
        if "_http_error" in one_day or "_url_error" in one_day or "_error" in one_day:
            result = {"status": "error", "error": one_day, "last_check": now_iso}
            state["last_check"] = now_iso
            save_state(state)
            write_output(result)
            print(json.dumps(result))
            return

    sessions_yesterday = extract_session_count(one_day) if isinstance(one_day, list) else 0
    new_cumulative = state["cumulative_sessions"] + sessions_yesterday
    previous_milestone = state["last_milestone"]
    current_milestone = (new_cumulative // MILESTONE_STEP) * MILESTONE_STEP

    crossed = (
        current_milestone > previous_milestone
        and current_milestone >= MILESTONE_STEP
    )

    result = {
        "current_sessions": new_cumulative,
        "previous_sessions": state["cumulative_sessions"],
        "sessions_yesterday": sessions_yesterday,
        "milestone": current_milestone,
        "previous_milestone": previous_milestone,
        "last_check": now_iso,
        "date": today,
    }

    if crossed:
        detailed = fetch_clarity(token, num_days=3, dimensions=["Device", "Country", "URL"])
        result["status"] = "milestone_hit"
        result["raw_insights"] = detailed
        report_path = write_report_skeleton(
            milestone=current_milestone,
            previous=previous_milestone,
            current=new_cumulative,
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
    print(json.dumps({k: v for k, v in result.items() if k != "raw_insights"}))


if __name__ == "__main__":
    main()
