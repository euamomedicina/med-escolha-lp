#!/usr/bin/env python3
"""
Clarity milestone checker.

Hits the Microsoft Clarity Data Export API ONCE per run with num_days=3 and
tracks a cumulative session count via positive-delta accumulation across runs.

The Clarity API only exposes a 1-3 day rolling window with no native cumulative
or per-day breakdown. We approximate cumulative-since-tracking-began by:

  seed (first run)  = env CLARITY_INITIAL_SESSIONS OR the first observed window
  cumulative        = seed + sum of positive deltas (window_today - window_yesterday)

If the window shrinks between runs (older day fell out, newer day was smaller),
delta is treated as 0 instead of negative (we never decrement). This means the
cumulative is a slight undercount in those cases but never overshoots.

Milestone fires when cumulative crosses a new MILESTONE_STEP multiple (1000).

Token loading:
  1. env CLARITY_API_TOKEN (preferred, used in CI)
  2. file ~/.config/clarity/api_token (local dev fallback)

Outputs:
  - stdout: compact JSON summary
  - <root>/clarity-output.json: full JSON payload (consumed by workflow)
  - <root>/last-clarity-payload.json: raw Clarity API response (gitignored, debug)
  - <root>/reports/clarity-YYYY-MM-DD.md: skeleton report (only on milestone_hit)

Status values:
  - "no_data"      Clarity returned 5xx/empty (no traffic yet).
  - "rate_limited" Clarity returned 429. Retry next run.
  - "no_milestone" Traffic exists but cumulative didn't cross a new 1000-multiple.
  - "milestone_hit" Crossed milestone. Workflow creates issue + commits report.
  - "error"        Unexpected error.
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
DEBUG_PAYLOAD_FILE = PROJECT_ROOT / "last-clarity-payload.json"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOCAL_TOKEN_FILE = Path.home() / ".config" / "clarity" / "api_token"
API_BASE = "https://www.clarity.ms/export-data/api/v1/project-live-insights"

MILESTONE_STEP = 1000
# URL filter: disabled by default. Project Clarity setup for med-escolha only
# tracks one domain so totalSessionCount is already our domain. Override via
# CLARITY_URL_FILTER env var if you ever add multi-domain to the same project.
DEFAULT_URL_FILTER = ""


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


def _empty_state():
    return {
        "cumulative_estimated": 0,
        "peak_window_3d": 0,
        "last_seen_window_3d": 0,
        "last_milestone": 0,
        "last_check": None,
        "last_report": None,
        "seeded_at": None,
        "seed_value": None,
        "history": [],  # list of {date, window_3d, delta, cumulative}
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return _empty_state()
    state = json.loads(STATE_FILE.read_text())
    # Defaults for the new shape (migrate older shapes silently).
    state.setdefault("cumulative_estimated", 0)
    state.setdefault("peak_window_3d", state.get("peak_sessions_3d", 0))
    state.setdefault("last_seen_window_3d", state.get("last_seen_sessions_3d", 0))
    state.setdefault("last_milestone", 0)
    state.setdefault("last_check", None)
    state.setdefault("last_report", None)
    state.setdefault("seeded_at", None)
    state.setdefault("seed_value", None)
    state.setdefault("history", [])
    # Strip legacy fields nobody reads anymore.
    for legacy in ("cumulative_sessions", "daily_counts", "peak_sessions_3d", "last_seen_sessions_3d"):
        state.pop(legacy, None)
    # Normalize old history entries (key was sessions_3d, now window_3d).
    new_history = []
    for entry in state["history"]:
        if not isinstance(entry, dict):
            continue
        normalized = {
            "date": entry.get("date"),
            "window_3d": entry.get("window_3d", entry.get("sessions_3d", 0)),
            "delta": entry.get("delta", 0),
            "cumulative": entry.get("cumulative", 0),
        }
        new_history.append(normalized)
    state["history"] = new_history
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def write_output(payload: dict) -> None:
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2))


def write_debug_payload(payload) -> None:
    try:
        DEBUG_PAYLOAD_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    except Exception:
        pass


def fetch_clarity(token: str, num_days: int = 3, dimensions=None):
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
    return [
        v for k, v in row.items()
        if isinstance(v, str) and k.lower() in ("url", "pageurl", "page_url")
    ]


def extract_session_count(payload, url_filter: str = None):
    """Sum sessions from a Clarity Traffic metric.

    Field priority: totalSessionCount, sessionsCount, numOfSessions, sessions.
    Values may be strings ("823") or ints (823); both handled.
    Returns (count, debug_info).
    """
    debug = {
        "any_url_seen": False,
        "matched_urls": set(),
        "skipped_urls": set(),
        "rows_total": 0,
        "rows_counted": 0,
        "metric_names_seen": [],
    }
    if not isinstance(payload, list):
        return 0, debug

    for metric in payload:
        if not isinstance(metric, dict):
            continue
        name = metric.get("metricName", "")
        if name and name not in debug["metric_names_seen"]:
            debug["metric_names_seen"].append(name)
        if name not in ("Traffic", "TrafficMetrics"):
            continue
        for row in metric.get("information", []) or []:
            if isinstance(row, dict) and _row_url_fields(row):
                debug["any_url_seen"] = True
                break
        if debug["any_url_seen"]:
            break

    can_filter = bool(url_filter) and debug["any_url_seen"]

    total = 0
    for metric in payload:
        if not isinstance(metric, dict):
            continue
        if metric.get("metricName") not in ("Traffic", "TrafficMetrics"):
            continue
        for row in metric.get("information", []) or []:
            if not isinstance(row, dict):
                continue
            debug["rows_total"] += 1
            count = 0
            for key in ("totalSessionCount", "sessionsCount", "numOfSessions", "sessions"):
                if key in row:
                    try:
                        count = int(row[key])
                    except (ValueError, TypeError):
                        count = 0
                    break

            if not can_filter:
                total += count
                debug["rows_counted"] += 1
                continue

            urls = _row_url_fields(row)
            if any(url_filter in u for u in urls):
                total += count
                debug["rows_counted"] += 1
                for u in urls:
                    if url_filter in u:
                        debug["matched_urls"].add(u)
            else:
                for u in urls:
                    debug["skipped_urls"].add(u)

    debug["matched_urls"] = sorted(debug["matched_urls"])
    debug["skipped_urls"] = sorted(debug["skipped_urls"])
    return total, debug


def write_report_skeleton(milestone, previous, cumulative, window_3d, history, insights, output_date):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"clarity-{output_date}.md"
    history_lines = "\n".join(
        f"- **{entry['date']}:** janela 3d = {entry['window_3d']} | acumulado = {entry['cumulative']} (delta +{entry['delta']})"
        for entry in history[-14:]
    )
    body = f"""# Relatório Clarity, {output_date}

**Milestone atingido:** {milestone} sessões acumuladas (anterior: {previous}).
**Acumulado estimado:** {cumulative} sessões.
**Janela 3d atual:** {window_3d} sessões.

## Histórico das checagens (últimas 14)

{history_lines or '- (sem histórico ainda)'}

> Acumulado estimado = seed inicial + soma dos deltas positivos entre janelas.
> Pra dados completos por dia, abra o dashboard do Clarity: https://clarity.microsoft.com

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

    # Optional seed (used only on the very first run when history is empty).
    seed_env = os.environ.get("CLARITY_INITIAL_SESSIONS", "").strip()
    seed_override = None
    if seed_env:
        try:
            seed_override = int(seed_env)
        except ValueError:
            seed_override = None

    payload = fetch_clarity(token, num_days=3, dimensions=None)
    write_debug_payload(payload)

    # Special responses (rate limit, no data, network errors).
    if isinstance(payload, dict):
        code = payload.get("_http_error")
        if code == 429:
            result = {
                "status": "rate_limited",
                "http_status": 429,
                "cumulative_estimated": state["cumulative_estimated"],
                "last_seen_window_3d": state["last_seen_window_3d"],
                "peak_window_3d": state["peak_window_3d"],
                "last_milestone": state["last_milestone"],
                "last_check": now_iso,
                "message": "Clarity rate-limited (429). Will retry next run.",
            }
            state["last_check"] = now_iso
            save_state(state)
            write_output(result)
            print(json.dumps(result))
            return
        if code in (500, 404) or payload.get("_empty"):
            result = {
                "status": "no_data",
                "http_status": code,
                "cumulative_estimated": state["cumulative_estimated"],
                "last_seen_window_3d": state["last_seen_window_3d"],
                "peak_window_3d": state["peak_window_3d"],
                "last_milestone": state["last_milestone"],
                "last_check": now_iso,
                "message": "Clarity returned no data.",
            }
            state["last_check"] = now_iso
            save_state(state)
            write_output(result)
            print(json.dumps(result))
            return
        if "_url_error" in payload or "_error" in payload:
            result = {"status": "error", "error": payload, "last_check": now_iso}
            state["last_check"] = now_iso
            save_state(state)
            write_output(result)
            print(json.dumps(result))
            return

    window_3d, debug = extract_session_count(payload, url_filter=url_filter)

    # Cumulative logic
    is_first_run = not state["history"]
    if is_first_run:
        seed = seed_override if seed_override is not None else window_3d
        cumulative = seed
        delta = 0
        state["seeded_at"] = today
        state["seed_value"] = seed
    else:
        previous_window = state["history"][-1]["window_3d"]
        delta = max(0, window_3d - previous_window)
        cumulative = state["cumulative_estimated"] + delta

    peak = max(state["peak_window_3d"], window_3d)
    previous_milestone = state["last_milestone"]
    current_milestone = (cumulative // MILESTONE_STEP) * MILESTONE_STEP
    crossed = current_milestone > previous_milestone and current_milestone >= MILESTONE_STEP

    state["history"].append({
        "date": today,
        "window_3d": window_3d,
        "delta": delta,
        "cumulative": cumulative,
    })
    state["history"] = state["history"][-60:]

    result = {
        "window_3d": window_3d,
        "cumulative_estimated": cumulative,
        "delta_today": delta,
        "peak_window_3d": peak,
        "previous_cumulative": state["cumulative_estimated"],
        "seed_value": state["seed_value"],
        "seeded_at": state["seeded_at"],
        "url_filter_active": bool(url_filter and debug["any_url_seen"]),
        "url_filter_requested": url_filter,
        "matched_urls": debug["matched_urls"],
        "skipped_urls": debug["skipped_urls"],
        "rows_in_payload": debug["rows_total"],
        "rows_counted": debug["rows_counted"],
        "metric_names_seen": debug["metric_names_seen"],
        "milestone": current_milestone,
        "previous_milestone": previous_milestone,
        "last_check": now_iso,
        "date": today,
    }

    if crossed:
        result["status"] = "milestone_hit"
        result["raw_insights"] = payload
        report_path = write_report_skeleton(
            milestone=current_milestone,
            previous=previous_milestone,
            cumulative=cumulative,
            window_3d=window_3d,
            history=state["history"],
            insights=payload,
            output_date=today,
        )
        result["report_path"] = str(report_path.relative_to(PROJECT_ROOT))
        state["last_milestone"] = current_milestone
        state["last_report"] = today
    else:
        result["status"] = "no_milestone"

    state["cumulative_estimated"] = cumulative
    state["peak_window_3d"] = peak
    state["last_seen_window_3d"] = window_3d
    state["last_check"] = now_iso
    save_state(state)
    write_output(result)

    print(json.dumps({k: v for k, v in result.items() if k != "raw_insights"}))


if __name__ == "__main__":
    main()
