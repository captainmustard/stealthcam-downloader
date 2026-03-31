#!/usr/bin/env python3
"""
Trail cam gallery downloader for stealthcamcommand.com
Usage:
    python3 download_trailcam.py
    python3 download_trailcam.py --output ~/TrailCam --headless
"""

import asyncio
import argparse
import getpass
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

try:
    from playwright.async_api import async_playwright, Response
except ImportError:
    print("Playwright not installed. Run:")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)

try:
    import httpx
except ImportError:
    httpx = None

import urllib.request


# ── helpers ──────────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


def download_file(url: str, dest: Path, headers: dict | None = None) -> bool:
    """Download a single file; returns True on success."""
    try:
        if httpx:
            with httpx.Client(follow_redirects=True, timeout=60) as client:
                resp = client.get(url, headers=headers or {})
                resp.raise_for_status()
                dest.write_bytes(resp.content)
        else:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=60) as r:
                dest.write_bytes(r.read())
        return True
    except Exception as e:
        print(f"  [!] Failed to download {url}: {e}")
        return False


# ── main ─────────────────────────────────────────────────────────────────────

async def run(email: str, password: str, output_dir: Path, headless: bool):
    output_dir.mkdir(parents=True, exist_ok=True)

    image_urls: set[str] = set()
    api_responses: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        # Capture every network response that looks like image/gallery API data
        async def on_response(response: Response):
            url = response.url
            ct = response.headers.get("content-type", "")

            # Grab JSON API responses (may contain image URLs)
            if "json" in ct:
                try:
                    body = await response.json()
                    api_responses.append({"url": url, "body": body})
                    # Walk the JSON looking for image URL strings
                    extract_urls_from_json(body, image_urls)
                except Exception:
                    pass

            # Also capture direct image URLs served by the CDN
            if any(ct.startswith(t) for t in ("image/jpeg", "image/png", "image/webp", "image/gif")):
                image_urls.add(url)

        page.on("response", on_response)

        # ── step 1: navigate to login page and wait for React to render ──
        print("[*] Opening login page …")
        await page.goto("https://stealthcamcommand.com/login", wait_until="networkidle")

        # Wait for the email input to actually appear in the React-rendered DOM
        print("[*] Waiting for login form to render …")
        try:
            await page.wait_for_selector('input[type="text"], input[type="email"], input[type="password"]',
                                         timeout=15000)
        except Exception:
            screenshot = output_dir / "_login_page.png"
            await page.screenshot(path=str(screenshot))
            print(f"  [!] Could not find email field. Screenshot saved: {screenshot}")
            print(f"  [!] Current URL: {page.url}")
            # Dump all input elements found
            inputs = await page.eval_on_selector_all("input", "els => els.map(e => ({type:e.type,name:e.name,id:e.id,placeholder:e.placeholder}))")
            print(f"  [!] Inputs found: {inputs}")
            await browser.close()
            return

        # Fill email — the field is type="text" with no name/id/placeholder
        print("[*] Filling login form …")
        for sel in ['input[type="email"]', 'input[name="email"]',
                    'input[placeholder*="email" i]', 'input[placeholder*="user" i]',
                    'input[type="text"]']:
            try:
                el = page.locator(sel).first
                if await el.is_visible():
                    await el.fill(email)
                    print(f"  [+] Filled email using: {sel}")
                    break
            except Exception:
                pass

        # Fill password
        for sel in ['input[type="password"]', 'input[name="password"]',
                    'input[placeholder*="password" i]']:
            try:
                el = page.locator(sel).first
                if await el.is_visible():
                    await el.fill(password)
                    print(f"  [+] Filled password using: {sel}")
                    break
            except Exception:
                pass

        # Submit
        submitted = False
        for sel in ['button[type="submit"]', 'input[type="submit"]',
                    'button:has-text("Sign In")', 'button:has-text("Log In")',
                    'button:has-text("Login")', 'button:has-text("Continue")']:
            try:
                el = page.locator(sel).first
                if await el.is_visible():
                    await el.click()
                    print(f"  [+] Submitted using: {sel}")
                    submitted = True
                    break
            except Exception:
                pass

        if not submitted:
            # Last resort: press Enter in the password field
            await page.keyboard.press("Enter")
            print("  [+] Submitted via Enter key")

        # Wait for redirect away from /login
        try:
            await page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        except Exception:
            screenshot = output_dir / "_after_login.png"
            await page.screenshot(path=str(screenshot))
            print(f"  [!] Still on login page after submit. Screenshot: {screenshot}")
            print(f"  [!] URL: {page.url}")

        await page.wait_for_load_state("networkidle")
        print(f"[*] After login — URL: {page.url}")

        # ── step 2: navigate to gallery ──
        print("[*] Navigating to gallery …")
        await page.goto("https://stealthcamcommand.com/gallery", wait_until="networkidle")

        # Scroll to the bottom repeatedly to trigger lazy-loading
        print("[*] Scrolling to load all images …")
        prev_height = 0
        for _ in range(50):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)
            height = await page.evaluate("document.body.scrollHeight")
            if height == prev_height:
                break
            prev_height = height

        # Click "Load more" buttons if present
        for _ in range(30):
            found = False
            for sel in ['button:has-text("Load More")', 'button:has-text("Show More")',
                        'a:has-text("Load More")', '[class*="load-more"]']:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible():
                        await el.click()
                        await page.wait_for_load_state("networkidle")
                        found = True
                        break
                except Exception:
                    pass
            if not found:
                break

        # Also collect <img> src values visible on the page
        img_srcs = await page.eval_on_selector_all(
            "img",
            "els => els.map(e => e.src).filter(s => s && !s.startsWith('data:'))"
        )
        for src in img_srcs:
            if looks_like_photo(src):
                image_urls.add(src)

        await browser.close()

    # ── step 3: save API responses for inspection ──
    api_log = output_dir / "_api_responses.json"
    api_log.write_text(json.dumps(api_responses, indent=2, default=str))
    print(f"[*] Saved {len(api_responses)} API response(s) to {api_log}")

    # ── step 4: download images ──
    photo_urls = [u for u in image_urls if looks_like_photo(u)]
    print(f"[*] Found {len(photo_urls)} image URL(s). Downloading to {output_dir} …")

    # Build a set of base stems already on disk (strips leading index and extension)
    def stem_key(name: str) -> str:
        """'0042_abc-def.JPG' -> 'abc-def'"""
        s = Path(name).stem  # drop extension
        return s.split("_", 1)[-1] if "_" in s else s

    seen_stems: set[str] = {stem_key(f.name) for f in output_dir.iterdir() if f.is_file()}

    ok = fail = 0
    for i, url in enumerate(sorted(photo_urls), 1):
        filename = url_to_filename(url, i)
        dest = output_dir / filename
        key = stem_key(filename)

        if dest.exists() or key in seen_stems:
            print(f"  [{i}/{len(photo_urls)}] skip (duplicate): {filename}")
            ok += 1
            continue

        print(f"  [{i}/{len(photo_urls)}] {filename}")
        if download_file(url, dest):
            seen_stems.add(key)
            ok += 1
        else:
            fail += 1

    print(f"\n[done] {ok} downloaded, {fail} failed.")
    if fail:
        print("       Check _api_responses.json for raw API data if images are missing.")

    # ── step 5: build WebM ──
    make_webm(output_dir, fps=3)


def make_webm(output_dir: Path, fps: int = 3):
    import subprocess
    import tempfile

    photo_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    frame_paths = sorted(
        f for f in output_dir.iterdir()
        if f.suffix.lower() in photo_exts and not f.name.startswith("_")
    )
    if not frame_paths:
        print("[!] No images found for WebM.")
        return

    print(f"[*] Building WebM from {len(frame_paths)} frame(s) at {fps} fps …")

    # Write a file list for ffmpeg concat demuxer
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as flist:
        for p in frame_paths:
            flist.write(f"file '{p.resolve()}'\n")
            flist.write(f"duration {1/fps:.6f}\n")
        flist_path = flist.name

    webm_path = output_dir / "trailcam.webm"
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", flist_path,
        "-vf", "scale=1280:-2",
        "-c:v", "libvpx-vp9", "-crf", "33", "-b:v", "0",
        str(webm_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(flist_path)

    if result.returncode != 0:
        print(f"[!] ffmpeg failed:\n{result.stderr}")
        return

    size_mb = webm_path.stat().st_size / 1_048_576
    print(f"[*] Saved {webm_path} ({size_mb:.1f} MB)")


# ── utilities ─────────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}

def looks_like_photo(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    ext = os.path.splitext(path)[1]
    if ext not in IMAGE_EXTENSIONS:
        return False
    basename = os.path.basename(path)
    return "_thumb" not in basename and "thumb_" not in basename


def url_to_filename(url: str, index: int) -> str:
    parsed = urllib.parse.urlparse(url)
    basename = os.path.basename(parsed.path) or f"image_{index:04d}.jpg"
    # Avoid collisions by prefixing index
    name, ext = os.path.splitext(basename)
    return sanitize_filename(f"{index:04d}_{name}{ext}")


def extract_urls_from_json(obj, found: set, depth: int = 0):
    """Recursively walk JSON looking for strings that look like image URLs."""
    if depth > 20:
        return
    if isinstance(obj, str):
        if obj.startswith("http") and looks_like_photo(obj):
            found.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            extract_urls_from_json(v, found, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            extract_urls_from_json(item, found, depth + 1)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download trail cam photos from stealthcamcommand.com")
    parser.add_argument("--email",    help="Account email (prompted if omitted)")
    parser.add_argument("--password", help="Account password (prompted if omitted)")
    parser.add_argument("--output",   default="./trailcam_photos",
                        help="Destination folder (default: ./trailcam_photos)")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser in headless mode (no window)")
    args = parser.parse_args()

    email    = args.email    or input("Email: ")
    password = args.password or getpass.getpass("Password: ")

    asyncio.run(run(
        email=email,
        password=password,
        output_dir=Path(args.output).expanduser(),
        headless=args.headless,
    ))


if __name__ == "__main__":
    main()
