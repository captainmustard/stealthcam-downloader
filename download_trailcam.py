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
import shutil
import urllib.parse
from pathlib import Path

try:
    from playwright.async_api import async_playwright, Response, Page
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

async def download_file(url: str, dest: Path, client=None, headers: dict | None = None) -> bool:
    """Download a single file; returns True on success."""
    try:
        if client:
            resp = await client.get(url, headers=headers or {})
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        else:
            req = urllib.request.Request(url, headers=headers or {})
            with await asyncio.to_thread(urllib.request.urlopen, req, 60) as r:
                dest.write_bytes(r.read())
        return True
    except Exception as e:
        print(f"  [!] Failed to download {url}: {e}")
        return False

# ── modular async functions ──────────────────────────────────────────────────

async def login(page: Page, email: str, password: str, output_dir: Path) -> bool:
    print("[*] Opening login page …")
    await page.goto("https://stealthcamcommand.com/login", wait_until="networkidle")

    print("[*] Waiting for login form to render …")
    try:
        await page.wait_for_selector('input[type="text"], input[type="email"], input[type="password"]', timeout=15000)
    except Exception:
        screenshot = output_dir / "_login_page.png"
        await page.screenshot(path=str(screenshot))
        print(f"  [!] Could not find email field. Screenshot saved: {screenshot}")
        return False

    # Fill email
    print("[*] Filling login form …")
    for sel in ['input[type="email"]', 'input[name="email"]', 'input[placeholder*="email" i]', 'input[placeholder*="user" i]', 'input[type="text"]']:
        try:
            el = page.locator(sel).first
            if await el.is_visible():
                await el.fill(email)
                print(f"  [+] Filled email using: {sel}")
                break
        except Exception:
            pass

    # Fill password
    for sel in ['input[type="password"]', 'input[name="password"]', 'input[placeholder*="password" i]']:
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
    for sel in ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("Sign In")', 'button:has-text("Log In")', 'button:has-text("Login")', 'button:has-text("Continue")']:
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
        await page.keyboard.press("Enter")
        print("  [+] Submitted via Enter key")

    # Wait for redirect
    try:
        await page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
    except Exception:
        screenshot = output_dir / "_after_login.png"
        await page.screenshot(path=str(screenshot))
        print(f"  [!] Still on login page after submit. Screenshot: {screenshot}")
        return False

    await page.wait_for_load_state("networkidle")
    print(f"[*] After login — URL: {page.url}")
    return True

async def scrape_gallery(page: Page, image_urls: set):
    print("[*] Navigating to gallery …")
    await page.goto("https://stealthcamcommand.com/gallery", wait_until="networkidle")

    print("[*] Scrolling to load all images …")
    prev_height = 0
    for _ in range(50):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        height = await page.evaluate("document.body.scrollHeight")
        if height == prev_height:
            break
        prev_height = height

    for _ in range(30):
        found = False
        for sel in ['button:has-text("Load More")', 'button:has-text("Show More")', 'a:has-text("Load More")', '[class*="load-more"]']:
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

    # Collect visible <img> src values
    img_srcs = await page.eval_on_selector_all(
        "img",
        "els => els.map(e => e.src).filter(s => s && !s.startsWith('data:'))"
    )
    for src in img_srcs:
        if looks_like_photo(src):
            image_urls.add(src)

async def download_worker(semaphore, url, dest, filename, index, total, client, seen_stems, stem_key_fn, lock):
    async with semaphore:
        key = stem_key_fn(filename)
        async with lock:
            if key in seen_stems:
                print(f"  [{index}/{total}] skip (duplicate): {filename}")
                return "skipped"
            seen_stems.add(key)

        success = await download_file(url, dest, client)
        if success:
            print(f"  [{index}/{total}] Downloaded: {filename}")
            return "downloaded"
        else:
            async with lock:
                seen_stems.discard(key)
            return "failed"


# ── main ─────────────────────────────────────────────────────────────────────

async def run(email: str, password: str, output_dir: Path, headless: bool):
    output_dir.mkdir(parents=True, exist_ok=True)

    image_urls: set[str] = set()
    api_responses: list[dict] = []
    success = False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        async def on_response(response: Response):
            url = response.url
            ct = response.headers.get("content-type", "")

            if "json" in ct:
                try:
                    body = await response.json()
                    api_responses.append({"url": url, "body": body})
                    extract_urls_from_json(body, image_urls)
                except Exception:
                    pass

            if any(ct.startswith(t) for t in ("image/jpeg", "image/png", "image/webp", "image/gif")):
                image_urls.add(url)

        page.on("response", on_response)

        success = await login(page, email, password, output_dir)
        if success:
            await scrape_gallery(page, image_urls)

        await browser.close()

    if not success:
        print("[!] Aborting due to login failure.")
        return

    # Save API responses
    api_log = output_dir / "_api_responses.json"
    api_log.write_text(json.dumps(api_responses, indent=2, default=str))
    print(f"[*] Saved {len(api_responses)} API response(s) to {api_log}")

    # Process and download images concurrently
    photo_urls = sorted([u for u in image_urls if looks_like_photo(u)])
    total_photos = len(photo_urls)
    print(f"[*] Found {total_photos} image URL(s). Downloading to {output_dir} …")

    def stem_key(name: str) -> str:
        s = Path(name).stem
        return s.split("_", 1)[-1] if "_" in s else s

    seen_stems: set[str] = {stem_key(f.name) for f in output_dir.iterdir() if f.is_file()}
    semaphore = asyncio.Semaphore(10)
    lock = asyncio.Lock()
    tasks = []
    skipped = 0

    client_cm = httpx.AsyncClient(follow_redirects=True, timeout=60) if httpx else None

    async with (client_cm or _null_context()) as client:
        for i, url in enumerate(photo_urls, 1):
            filename = url_to_filename(url, i)
            dest = output_dir / filename
            key = stem_key(filename)

            if dest.exists() or key in seen_stems:
                print(f"  [{i}/{total_photos}] skip (duplicate): {filename}")
                seen_stems.add(key)
                skipped += 1
                continue

            tasks.append(download_worker(semaphore, url, dest, filename, i, total_photos, client, seen_stems, stem_key, lock))

        results = await asyncio.gather(*tasks) if tasks else []

    ok = results.count("downloaded")
    skipped += results.count("skipped")
    fail = results.count("failed")

    print(f"\n[done] {ok} downloaded, {skipped} skipped, {fail} failed.")
    if fail:
        print("        Check _api_responses.json for raw API data if images are missing.")

    make_webm(output_dir, fps=3)


class _null_context:
    """Async context manager that yields None (stands in when httpx is unavailable)."""
    async def __aenter__(self): return None
    async def __aexit__(self, *_): pass


def make_webm(output_dir: Path, fps: int = 3):
    import subprocess
    import tempfile

    if not shutil.which("ffmpeg"):
        print("[!] FFmpeg is not installed or not in PATH. Skipping WebM generation.")
        return

    photo_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    frame_paths = sorted(
        f for f in output_dir.iterdir()
        if f.suffix.lower() in photo_exts and not f.name.startswith("_")
    )
    if not frame_paths:
        print("[!] No images found for WebM.")
        return

    print(f"[*] Building WebM from {len(frame_paths)} frame(s) at {fps} fps …")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as flist:
        for p in frame_paths:
            safe_path = str(p.resolve()).replace("'", "'\\''")
            flist.write(f"file '{safe_path}'\n")
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

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
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
    name, ext = os.path.splitext(basename)
    return sanitize_filename(f"{index:04d}_{name}{ext}")


def extract_urls_from_json(obj, found: set, depth: int = 0):
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
