#!/usr/bin/env python3
"""
generate_videos.py — Med Escolha video batch generator via kie.ai Veo 3 Fast.

Reads config/videos.json (manifest of prompts), fires each to Veo3 Fast,
downloads MP4s to videos/ (organized per concept).

Usage:
    python3 scripts/generate_videos.py                    # all videos
    python3 scripts/generate_videos.py --ids 01,02,03     # only specific ids
    python3 scripts/generate_videos.py --smoke            # just the first
    python3 scripts/generate_videos.py --model veo3       # standard Veo3 (more expensive)

API key path: ~/.config/kie/api_key (chmod 600)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
ROOT = SCRIPT_DIR.parent  # ads/med-escolha/
VIDEOS_DIR = ROOT / "videos"
MANIFEST_FILE = ROOT / "config" / "videos.json"

KEY_FILE = Path.home() / ".config" / "kie" / "api_key"

SUBMIT_URL = "https://api.kie.ai/api/v1/veo/generate"
STATUS_URL = "https://api.kie.ai/api/v1/veo/record-info"

DEFAULT_MODEL = "veo3_fast"  # veo3_fast = ~$0.20/clip · veo3 = ~$0.50/clip
POLL_INTERVAL_S = 10
POLL_TIMEOUT_S = 600  # Veo can take 1-5 min


def load_api_key() -> str:
    if not KEY_FILE.exists():
        sys.exit(f"error: api key not found at {KEY_FILE}")
    return KEY_FILE.read_text().strip()


def submit_video(api_key: str, model: str, prompt: str, aspect_ratio: str = "9:16") -> str:
    body = {
        "model": model,
        "prompt": prompt,
        "aspectRatio": aspect_ratio,
        "enableFallback": True,
    }
    r = requests.post(
        SUBMIT_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    body_json = r.json()
    if body_json.get("code") != 200:
        raise RuntimeError(f"submit error {body_json.get('code')}: {body_json.get('msg')}")
    task_id = (body_json.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"no taskId in response: {body_json}")
    return task_id


def poll_video(api_key: str, task_id: str) -> str:
    """Poll until done and return MP4 URL."""
    deadline = time.time() + POLL_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        r = requests.get(
            STATUS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            params={"taskId": task_id},
            timeout=30,
        )
        if r.status_code != 200:
            continue
        data = r.json().get("data") or {}
        flag = data.get("successFlag")
        if flag == 1:
            response = data.get("response") or {}
            urls = response.get("resultUrls") or []
            if urls:
                return urls[0]
            raise RuntimeError(f"successFlag=1 but no resultUrls: {data}")
        if flag in (2, 3) or data.get("errorCode"):
            raise RuntimeError(f"video job failed: {data.get('errorMessage') or data}")
    raise TimeoutError(f"video job {task_id} did not finish within {POLL_TIMEOUT_S}s")


def download_video(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=300, stream=True)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Med Escolha videos via kie.ai Veo3")
    parser.add_argument("--ids", type=str, default="", help="Comma-separated video ids to run")
    parser.add_argument("--smoke", action="store_true", help="Only run the first video")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help="veo3 or veo3_fast")
    args = parser.parse_args()

    api_key = load_api_key()
    manifest = json.loads(MANIFEST_FILE.read_text())
    videos = manifest["videos"]

    if args.smoke:
        selected = videos[:1]
    elif args.ids.strip():
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        selected = [v for v in videos if v["id"] in wanted]
    else:
        selected = videos

    print(f"running {len(selected)} video(s) | model={args.model}")
    print(f"estimated cost: ~${len(selected) * (0.20 if args.model == 'veo3_fast' else 0.50):.2f}")

    saved = []
    errors = []
    for v in selected:
        vid = v["id"]
        name = v["name"]
        prompt = v["prompt"]
        category = v.get("category", "misc")

        out_dir = VIDEOS_DIR / category / f"{vid}-{name}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "prompt.txt").write_text(prompt)

        print(f"\n[{vid}] {name} ({category})")
        try:
            print(f"  → submitting (model={args.model})...")
            task_id = submit_video(api_key, args.model, prompt, v.get("aspect_ratio", "9:16"))
            print(f"     task_id={task_id}, polling...")
            url = poll_video(api_key, task_id)
            dest = out_dir / "video.mp4"
            download_video(url, dest)
            saved.append(dest)
            print(f"     ✓ saved {dest.relative_to(ROOT)}")
        except Exception as e:
            print(f"  ! ERROR: {e}")
            errors.append(f"{vid}: {e}")

    print(f"\n=== done. saved {len(saved)} video(s). errors: {len(errors)}")
    for e in errors:
        print(f"  - {e}")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
