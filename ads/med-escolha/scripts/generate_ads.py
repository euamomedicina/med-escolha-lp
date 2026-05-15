#!/usr/bin/env python3
"""
generate_ads.py — Med Escolha ad batch generator via kie.ai (Nano Banana 2).

Reads prompts.json, fires each prompt to kie.ai, downloads images to outputs/.

Usage:
    python generate_ads.py                       # all templates, 2 images each
    python generate_ads.py --templates 1,4,7     # only templates 1, 4, and 7
    python generate_ads.py --images 4            # 4 images per template
    python generate_ads.py --smoke               # smoke test: only first template, 1 image

API key path: ~/.config/kie/api_key (chmod 600)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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
PRODUCT_IMAGES_DIR = ROOT / "assets"
OUTPUTS_DIR = ROOT / "singles"
PROMPTS_FILE = ROOT / "config" / "prompts.json"

KEY_FILE = Path.home() / ".config" / "kie" / "api_key"

# kie.ai endpoints (REST job model)
SUBMIT_URL = os.environ.get("KIE_SUBMIT_URL", "https://api.kie.ai/api/v1/jobs/createTask")
STATUS_URL = os.environ.get("KIE_STATUS_URL", "https://api.kie.ai/api/v1/jobs/recordInfo")
UPLOAD_URL = os.environ.get(
    "KIE_UPLOAD_URL",
    "https://kieai.redpandaai.co/api/file-base64-upload",
)

# Model identifiers on kie.ai (verified 2026-05-11)
#   nano-banana-2          → Nano Banana 2 text-to-image
#   google/nano-banana     → Nano Banana 1 text-to-image
#   google/nano-banana-edit → Nano Banana 1 image-edit (v2 edit not yet on kie.ai)
MODEL_TEXT_V2 = os.environ.get("KIE_MODEL_TEXT_V2", "nano-banana-2")
MODEL_TEXT_V1 = os.environ.get("KIE_MODEL_TEXT_V1", "google/nano-banana")
MODEL_EDIT = os.environ.get("KIE_MODEL_EDIT", "google/nano-banana-edit")

POLL_INTERVAL_S = 3
POLL_TIMEOUT_S = 240

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def load_api_key() -> str:
    if not KEY_FILE.exists():
        sys.exit(f"error: api key not found at {KEY_FILE}")
    return KEY_FILE.read_text().strip()


def upload_image_public(image_path: Path) -> str:
    """Upload a local image to a public host. Tries catbox.moe first, then 0x0.st."""
    print(f"  ↑ uploading {image_path.name}...", end=" ", flush=True)

    # Try 1: catbox.moe (most reliable)
    try:
        result = subprocess.run(
            [
                "curl", "-fsSL",
                "-F", "reqtype=fileupload",
                "-F", f"fileToUpload=@{image_path}",
                "https://catbox.moe/user/api.php",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            if url.startswith("http"):
                print(f"→ {url} (catbox)")
                return url
    except Exception:
        pass

    # Try 2: 0x0.st
    try:
        result = subprocess.run(
            ["curl", "-fsSL", "-F", f"file=@{image_path}", "https://0x0.st"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            if url.startswith("http"):
                print(f"→ {url} (0x0)")
                return url
    except Exception:
        pass

    raise RuntimeError(f"all image upload hosts failed for {image_path.name}")


def submit_job(api_key: str, model: str, payload_input: dict) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": model, "input": payload_input}
    r = requests.post(SUBMIT_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    body_json = r.json()
    if body_json.get("code") and body_json["code"] != 200:
        raise RuntimeError(f"kie.ai submit error {body_json.get('code')}: {body_json.get('msg')}")
    data = body_json.get("data") or {}
    task_id = data.get("taskId") or data.get("task_id") or body_json.get("taskId")
    if not task_id:
        raise RuntimeError(f"kie.ai submit returned no taskId: {body_json}")
    return task_id


def poll_job(api_key: str, task_id: str) -> list[str]:
    """Poll until job finishes and return list of result URLs."""
    headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.time() + POLL_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        r = requests.get(STATUS_URL, headers=headers, params={"taskId": task_id}, timeout=30)
        if r.status_code != 200:
            continue
        data = r.json().get("data") or {}
        state = (data.get("state") or data.get("status") or "").lower()
        if state in {"success", "completed", "done", "succeed"}:
            return extract_result_urls(data)
        if state in {"failed", "error", "fail"}:
            raise RuntimeError(f"kie.ai job failed: {data}")
        # else: waiting / queuing / generating — keep polling
    raise TimeoutError(f"kie.ai job {task_id} did not finish within {POLL_TIMEOUT_S}s")


def extract_result_urls(data: dict) -> list[str]:
    """kie.ai returns results in several shapes — try them all."""
    # Direct array
    for key in ("resultUrls", "outputs", "result_urls"):
        v = data.get(key)
        if isinstance(v, list) and v:
            return [u for u in v if isinstance(u, str)]
    # Single URL
    for key in ("output_url", "outputUrl", "result_url", "resultUrl"):
        v = data.get(key)
        if isinstance(v, str) and v:
            return [v]
    # Stringified JSON in resultJson
    rj = data.get("resultJson")
    if isinstance(rj, str):
        try:
            parsed = json.loads(rj)
            for key in ("resultUrls", "outputs"):
                v = parsed.get(key)
                if isinstance(v, list) and v:
                    return [u for u in v if isinstance(u, str)]
        except json.JSONDecodeError:
            pass
    raise RuntimeError(f"could not extract result urls from: {data}")


def download_image(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def run_prompt(
    api_key: str,
    prompt_obj: dict,
    image_url_cache: dict[str, str],
    images_per_prompt: int,
    use_v2: bool,
) -> list[Path]:
    """Generate N images for one prompt and save them. Returns list of saved paths."""
    n = prompt_obj["template_number"]
    name = prompt_obj["template_name"]
    needs_imgs = prompt_obj.get("needs_product_images", False)
    aspect = prompt_obj.get("aspect_ratio", "1:1")
    prompt_text = prompt_obj["prompt"]

    out_dir = OUTPUTS_DIR / f"{n:02d}-{name}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save the prompt for reference
    (out_dir / "prompt.txt").write_text(prompt_text + "\n")

    # Resolve image URLs if needed
    image_urls: list[str] = []
    if needs_imgs:
        for local_name in prompt_obj.get("image_urls", []):
            local_path = PRODUCT_IMAGES_DIR / local_name
            if not local_path.exists():
                raise FileNotFoundError(f"reference image not found: {local_path}")
            if local_name not in image_url_cache:
                image_url_cache[local_name] = upload_image_public(local_path)
            image_urls.append(image_url_cache[local_name])

    # Pick model — edit only has v1 on kie.ai today; text has v2.
    if needs_imgs:
        model = MODEL_EDIT
    else:
        model = MODEL_TEXT_V2 if use_v2 else MODEL_TEXT_V1

    saved_paths: list[Path] = []
    for i in range(1, images_per_prompt + 1):
        payload_input: dict = {
            "prompt": prompt_text,
            "aspect_ratio": aspect,
            "output_format": "png",
        }
        if image_urls:
            payload_input["image_urls"] = image_urls

        print(
            f"  → [{i}/{images_per_prompt}] submitting (model={model}, aspect={aspect})..."
        )
        task_id = submit_job(api_key, model, payload_input)
        print(f"     task_id={task_id}, polling...")
        urls = poll_job(api_key, task_id)
        if not urls:
            print(f"     ! no result urls")
            continue

        # Download first URL (most models return 1 image per job; if multiple, take first)
        url = urls[0]
        ext = Path(urlparse(url).path).suffix or ".png"
        dest = out_dir / f"v{i}{ext}"
        download_image(url, dest)
        saved_paths.append(dest)
        print(f"     ✓ saved {dest.relative_to(ROOT)}")

    return saved_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Med Escolha ads via kie.ai")
    parser.add_argument(
        "--templates",
        type=str,
        default="",
        help="Comma-separated template numbers to run (e.g. 1,4,7). Empty = all.",
    )
    parser.add_argument(
        "--images",
        type=int,
        default=2,
        help="Images to generate per template (default: 2).",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke test: only run template 1 with 1 image.",
    )
    parser.add_argument(
        "--v1",
        action="store_true",
        help="Use Nano Banana v1 instead of v2.",
    )
    args = parser.parse_args()

    api_key = load_api_key()
    prompts = json.loads(PROMPTS_FILE.read_text())["prompts"]

    if args.smoke:
        target_nums = {1}
        per_prompt = 1
    else:
        if args.templates.strip():
            target_nums = {int(x) for x in args.templates.split(",") if x.strip()}
        else:
            target_nums = {p["template_number"] for p in prompts}
        per_prompt = args.images

    use_v2 = not args.v1

    selected = [p for p in prompts if p["template_number"] in target_nums]
    print(
        f"running {len(selected)} template(s) × {per_prompt} image(s) "
        f"= {len(selected) * per_prompt} total | model={'v2' if use_v2 else 'v1'}"
    )

    image_url_cache: dict[str, str] = {}
    all_saved: list[Path] = []
    errors: list[str] = []

    for p in selected:
        print(
            f"\n[{p['template_number']:02d}] {p['template_name']} "
            f"(aspect={p['aspect_ratio']}, refs={p.get('needs_product_images', False)})"
        )
        try:
            saved = run_prompt(api_key, p, image_url_cache, per_prompt, use_v2)
            all_saved.extend(saved)
        except Exception as e:
            msg = f"[{p['template_number']:02d}] {p['template_name']}: {e}"
            print(f"  ! ERROR: {e}")
            errors.append(msg)

    print(f"\n=== done. saved {len(all_saved)} image(s). errors: {len(errors)}")
    for e in errors:
        print(f"  - {e}")

    # Build gallery
    if all_saved:
        build_gallery()

    return 0 if not errors else 1


def build_gallery() -> None:
    """Generate a simple HTML gallery of all outputs."""
    html_parts = [
        "<!doctype html>",
        '<html lang="pt-br"><head><meta charset="utf-8">',
        "<title>med escolha — ads gallery</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:0;padding:32px;background:#F5F7F9;color:#0E1F4D}",
        "h1{font-weight:600;margin-bottom:8px}h1 span{color:#1FBFA8}",
        ".sub{color:#6B7280;margin-bottom:32px;font-size:14px}",
        ".tpl{background:#fff;border-radius:16px;padding:24px;margin-bottom:24px;box-shadow:0 2px 8px rgba(14,31,77,.06)}",
        ".tpl h2{margin:0 0 12px 0;font-size:18px}.tpl .meta{color:#6B7280;font-size:13px;margin-bottom:16px}",
        ".imgs{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}",
        ".imgs img{width:100%;border-radius:12px;background:#eee;display:block}",
        ".prompt{margin-top:16px;padding:12px;background:#F9FAFB;border-radius:8px;font-size:12px;color:#4B5563;font-family:ui-monospace,Menlo,monospace;white-space:pre-wrap;max-height:120px;overflow:auto}",
        "</style></head><body>",
        "<h1>med <span>escolha</span> · ads gallery</h1>",
        '<p class="sub">geradas via kie.ai · nano banana 2</p>',
    ]
    for tpl_dir in sorted(OUTPUTS_DIR.iterdir()):
        if not tpl_dir.is_dir():
            continue
        imgs = sorted(p for p in tpl_dir.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
        if not imgs:
            continue
        prompt_file = tpl_dir / "prompt.txt"
        prompt = prompt_file.read_text() if prompt_file.exists() else ""
        html_parts.append(f'<div class="tpl"><h2>{tpl_dir.name}</h2>')
        html_parts.append(f'<div class="meta">{len(imgs)} image(s)</div>')
        html_parts.append('<div class="imgs">')
        for img in imgs:
            rel = img.relative_to(ROOT).as_posix()
            html_parts.append(f'<a href="{rel}" target="_blank"><img src="{rel}" loading="lazy"></a>')
        html_parts.append("</div>")
        if prompt:
            # Escape <
            esc = prompt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html_parts.append(f'<div class="prompt">{esc}</div>')
        html_parts.append("</div>")
    html_parts.append("</body></html>")
    (ROOT / "gallery.html").write_text("\n".join(html_parts))
    print(f"  → gallery: {ROOT / 'gallery.html'}")


if __name__ == "__main__":
    sys.exit(main())
