#!/usr/bin/env python3
"""
Trail cam gallery downloader for stealthcamcommand.com
Usage:
    python3 download_trailcam.py
    python3 download_trailcam.py --output ~/TrailCam --headless
    python3 download_trailcam.py --novideo
    python3 download_trailcam.py --format gif
    python3 download_trailcam.py --keep-old
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
from datetime import datetime
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
    """Download a single file asynchronously; returns True on success."""
    try:
        if client:
            resp = await client.get(url, headers=headers or {})
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        elif httpx:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as temp_client:
                resp = await temp_client.get(url, headers=headers or {})
                resp.raise_for_status()
                dest.write_bytes(resp.content)
        else:
            # Fallback for systems without httpx; wrap blocking urllib in to_thread
            def _urllib_download():
                req = urllib.request.Request(url, headers=headers or {})
                with urllib.request.urlopen(req, timeout=60) as r:
                    dest.write_bytes(r.read())
            await asyncio.to_thread(_urllib_download)
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
    prev_count = 0
    stable_rounds = 0
    for _ in range(200):
        # Scroll both the window and any scrollable inner containers
        await page.evaluate("""
            window.scrollTo(0, document.body.scrollHeight);
            document.querySelectorAll('*').forEach(el => {
                if (el.scrollHeight > el.clientHeight + 10) {
                    el.scrollTop = el.scrollHeight;
                }
            });
        """)
        await asyncio.sleep(1.5)
        count = len(await page.query_selector_all("img"))
        if count == prev_count:
            stable_rounds += 1
            if stable_rounds >= 3:
                break
        else:
            stable_rounds = 0
        prev_count = count
    print(f"[*] Detected {prev_count} image element(s) on page")

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
    """Wrapper to run the download function concurrently."""
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

async def run(email: str, password: str, output_dir: Path, headless: bool,
              novideo: bool, nophotos: bool, fmt: str, keep_old: bool, smoothing: str | None):
    output_dir.mkdir(parents=True, exist_ok=True)
    photos_dir = output_dir / "photos"
    photos_dir.mkdir(exist_ok=True)

    if not nophotos:
        image_urls: set[str] = set()
        api_responses: list[dict] = []
        success = False  # guard if exception fires before login()

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

        # Process and download images concurrently into photos_dir
        photo_urls = sorted([u for u in image_urls if looks_like_photo(u)])
        total_photos = len(photo_urls)
        print(f"[*] Found {total_photos} image URL(s). Downloading to {photos_dir} …")

        def stem_key(name: str) -> str:
            s = Path(name).stem
            return s.split("_", 1)[-1] if "_" in s else s

        seen_stems: set[str] = {stem_key(f.name) for f in photos_dir.iterdir() if f.is_file()}

        tasks = []
        semaphore = asyncio.Semaphore(10)
        lock = asyncio.Lock()

        client = httpx.AsyncClient(follow_redirects=True, timeout=60) if httpx else None

        ok = 0
        skipped = 0
        fail = 0

        try:
            for i, url in enumerate(photo_urls, 1):
                filename = url_to_filename(url, i)
                dest = photos_dir / filename
                key = stem_key(filename)

                if dest.exists() or key in seen_stems:
                    print(f"  [{i}/{total_photos}] skip (duplicate): {filename}")
                    skipped += 1
                    seen_stems.add(key)
                    continue

                tasks.append(download_worker(semaphore, url, dest, filename, i, total_photos, client, seen_stems, stem_key, lock))

            if tasks:
                results = await asyncio.gather(*tasks)
                ok += results.count("downloaded")
                skipped += results.count("skipped")
                fail += results.count("failed")
        finally:
            if client:
                await client.aclose()

        print(f"\n[done] {ok} downloaded, {skipped} skipped, {fail} failed.")
        if fail:
            print("        Check _api_responses.json for raw API data if images are missing.")

    if not novideo:
        make_video(photos_dir, output_dir, fmt=fmt, fps=3, keep_old=keep_old,
                   smoothing=smoothing)


# ── video export ──────────────────────────────────────────────────────────────

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

def frame_sort_key(p: Path) -> str:
    """Sort by UUID stem (strips leading NNNN_ index), which is time-based."""
    s = p.stem
    return s.split("_", 1)[-1] if "_" in s else s

def get_frame_paths(photos_dir: Path) -> list[Path]:
    return sorted(
        (f for f in photos_dir.iterdir()
         if f.suffix.lower() in PHOTO_EXTS and not f.name.startswith("_")),
        key=frame_sort_key,
    )

def uuid_timestamp_ms(stem: str) -> int | None:
    """Extract millisecond timestamp from a UUID v7 stem (strips leading NNNN_ index)."""
    uuid_part = stem.split("_", 1)[-1] if "_" in stem else stem
    hex_str = uuid_part.replace("-", "")[:12]
    try:
        return int(hex_str, 16)
    except ValueError:
        return None

def group_into_bursts(frame_paths: list[Path], gap_ms: int = 10_000) -> list[list[Path]]:
    """Group consecutive frames into bursts; a new burst starts after a gap > gap_ms."""
    if not frame_paths:
        return []
    bursts, current = [], [frame_paths[0]]
    prev_ts = uuid_timestamp_ms(frame_paths[0].stem)
    for p in frame_paths[1:]:
        ts = uuid_timestamp_ms(p.stem)
        if ts is None or prev_ts is None or (ts - prev_ts) > gap_ms:
            bursts.append(current)
            current = []
        current.append(p)
        prev_ts = ts
    bursts.append(current)
    return bursts

def optical_flow_interpolate(img1, img2, n: int = 3):
    """Return n frames interpolated between img1 and img2 using Farneback optical flow."""
    import cv2
    import numpy as np

    # Resize img2 to match img1 if needed
    if img1.shape != img2.shape:
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))

    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
    flow = cv2.calcOpticalFlowFarneback(
        gray1, gray2, None,
        pyr_scale=0.5, levels=3, winsize=15,
        iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
    )

    h, w = img1.shape[:2]
    y_coords, x_coords = np.mgrid[0:h, 0:w].astype(np.float32)
    results = []
    for i in range(1, n + 1):
        t = i / (n + 1)
        map_x = x_coords + flow[..., 0] * t
        map_y = y_coords + flow[..., 1] * t
        warped = cv2.remap(img1, map_x, map_y, cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REPLICATE)
        results.append(warped)
    return results

N_INTERP = 3  # synthetic frames inserted between each real pair within a burst

CROSSFADE_PAUSE_S = 0.5  # how long to hold each real photo before crossfading

def expand_with_optical_flow(frame_paths: list[Path], fps: int,
                              n_interp: int = N_INTERP) -> tuple[list[tuple[Path, float]], Path]:
    """Expand with optical-flow-warped intermediate frames; all frames get equal duration."""
    import cv2
    import tempfile

    frame_dur = 1 / (fps * (n_interp + 1))
    bursts = group_into_bursts(frame_paths)
    tmp_dir = Path(tempfile.mkdtemp())
    expanded: list[tuple[Path, float]] = []
    idx = 0

    for burst in bursts:
        for j, path in enumerate(burst):
            expanded.append((path, frame_dur))
            if j < len(burst) - 1:
                img1 = cv2.imread(str(path))
                img2 = cv2.imread(str(burst[j + 1]))
                if img1 is None or img2 is None:
                    continue
                interp_frames = optical_flow_interpolate(img1, img2, n_interp)
                for k, frame in enumerate(interp_frames):
                    out = tmp_dir / f"interp_{idx:06d}_{k:02d}.jpg"
                    cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                    expanded.append((out, frame_dur))
                idx += 1

    return expanded, tmp_dir

def expand_with_crossfade(frame_paths: list[Path], fps: int,
                          n_interp: int = N_INTERP) -> tuple[list[tuple[Path, float]], Path]:
    """Expand with crossfade-blended frames; real photos pause before transitioning."""
    from PIL import Image
    import tempfile

    interp_dur = 1 / (fps * (n_interp + 1))
    bursts = group_into_bursts(frame_paths)
    tmp_dir = Path(tempfile.mkdtemp())
    expanded: list[tuple[Path, float]] = []
    idx = 0

    for burst in bursts:
        for j, path in enumerate(burst):
            # Real photo: hold for the pause duration
            expanded.append((path, CROSSFADE_PAUSE_S))
            if j < len(burst) - 1:
                img1 = Image.open(path).convert("RGB")
                img2 = Image.open(burst[j + 1]).convert("RGB").resize(img1.size, Image.LANCZOS)
                for k in range(n_interp):
                    t = (k + 1) / (n_interp + 1)
                    blended = Image.blend(img1, img2, t)
                    out = tmp_dir / f"interp_{idx:06d}_{k:02d}.jpg"
                    blended.save(str(out), "JPEG", quality=95)
                    expanded.append((out, interp_dur))
                idx += 1

    return expanded, tmp_dir

def expand_frames(frame_paths: list[Path], smoothing: str,
                  fps: int) -> tuple[list[tuple[Path, float]], Path | None]:
    """Dispatch to the appropriate frame expansion method."""
    if smoothing == "optical-flow":
        try:
            return expand_with_optical_flow(frame_paths, fps)
        except ImportError:
            print("[!] opencv-python not installed. Run: pip install opencv-python")
    elif smoothing == "crossfade":
        try:
            return expand_with_crossfade(frame_paths, fps)
        except ImportError:
            print("[!] Pillow not installed. Run: pip install pillow")
    return [(p, 1 / fps) for p in frame_paths], None

def archive_existing(path: Path):
    """Rename an existing output file with a timestamp so it isn't overwritten."""
    if path.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archived = path.with_stem(f"{path.stem}_{ts}")
        path.rename(archived)
        print(f"[*] Archived old video to {archived.name}")

def make_video(photos_dir: Path, output_dir: Path, fmt: str, fps: int, keep_old: bool,
               smoothing: str | None = None):
    if fmt == "gif":
        make_gif(photos_dir, output_dir, fps=fps, keep_old=keep_old, smoothing=smoothing)
    else:
        make_ffmpeg_video(photos_dir, output_dir, fmt=fmt, fps=fps, keep_old=keep_old,
                          smoothing=smoothing)

def make_gif(photos_dir: Path, output_dir: Path, fps: int, keep_old: bool,
             smoothing: str | None = None):
    try:
        from PIL import Image
    except ImportError:
        print("[!] Pillow not installed. Run: pip install pillow")
        return

    raw_paths = get_frame_paths(photos_dir)
    if not raw_paths:
        print("[!] No images found for GIF.")
        return

    if smoothing:
        sequence, tmp_dir = expand_frames(raw_paths, smoothing, fps)
        print(f"[*] {smoothing} expanded to {len(sequence)} frame(s)")
    else:
        sequence = [(p, 1 / fps) for p in raw_paths]
        tmp_dir = None

    print(f"[*] Building GIF from {len(sequence)} frame(s) …")

    frames = []
    durations_ms = []
    for p, dur in sequence:
        try:
            img = Image.open(p).convert("RGB")
            img.thumbnail((1280, 960), Image.LANCZOS)
            frames.append(img)
            durations_ms.append(int(dur * 1000))
        except Exception as e:
            print(f"  [!] Skipping {p.name}: {e}")

    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not frames:
        print("[!] No frames could be loaded.")
        return

    gif_path = output_dir / "trailcam.gif"
    if keep_old:
        archive_existing(gif_path)

    frames[0].save(
        gif_path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations_ms,
        loop=0,
    )
    size_mb = gif_path.stat().st_size / 1_048_576
    print(f"[*] Saved {gif_path} ({size_mb:.1f} MB)")

def make_ffmpeg_video(photos_dir: Path, output_dir: Path, fmt: str, fps: int, keep_old: bool,
                      smoothing: str | None = None):
    import subprocess
    import tempfile

    if not shutil.which("ffmpeg"):
        print("[!] ffmpeg not found. Install it or use --novideo.")
        return

    raw_paths = get_frame_paths(photos_dir)
    if not raw_paths:
        print("[!] No images found for video.")
        return

    if smoothing:
        sequence, tmp_dir = expand_frames(raw_paths, smoothing, fps)
        print(f"[*] {smoothing} expanded to {len(sequence)} frame(s)")
    else:
        sequence = [(p, 1 / fps) for p in raw_paths]
        tmp_dir = None

    print(f"[*] Building {fmt.upper()} from {len(sequence)} frame(s) …")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as flist:
        for p, dur in sequence:
            safe_path = str(p.resolve()).replace("'", "'\\''")
            flist.write(f"file '{safe_path}'\n")
            flist.write(f"duration {dur:.6f}\n")
        flist_path = flist.name

    out_path = output_dir / f"trailcam.{fmt}"
    if keep_old:
        archive_existing(out_path)

    if fmt == "mp4":
        codec_args = ["-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p"]
    else:  # webm
        codec_args = ["-c:v", "libvpx-vp9", "-crf", "33", "-b:v", "0"]

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", flist_path,
        "-vf", "scale=1280:-2",
        *codec_args,
        str(out_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        os.unlink(flist_path)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if result.returncode != 0:
        print(f"[!] ffmpeg failed:\n{result.stderr}")
        return

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"[*] Saved {out_path} ({size_mb:.1f} MB)")


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
    parser.add_argument("--email",     help="Account email (prompted if omitted)")
    parser.add_argument("--password",  help="Account password (prompted if omitted)")
    parser.add_argument("--output",    default="./trailcam_photos",
                        help="Destination folder (default: ./trailcam_photos)")
    parser.add_argument("--headless",  action="store_true",
                        help="Run browser in headless mode (no window)")
    parser.add_argument("--novideo",   action="store_true",
                        help="Skip video creation")
    parser.add_argument("--nophotos",  action="store_true",
                        help="Skip downloading photos (useful for rebuilding video from existing photos)")
    parser.add_argument("--format",    choices=["mp4", "webm", "gif"], default="mp4",
                        help="Output video format (default: mp4)")
    parser.add_argument("--keep-old",  action="store_true",
                        help="Archive existing video with a timestamp instead of overwriting it")
    parser.add_argument("--smoothing", choices=["optical-flow", "crossfade"], default=None,
                        help="Smoothly interpolate between shots within each burst: "
                             "'optical-flow' warps pixels along tracked motion; "
                             "'crossfade' blends frames like a photo viewer transition")
    args = parser.parse_args()

    email    = args.email    or input("Email: ")
    password = args.password or getpass.getpass("Password: ")

    asyncio.run(run(
        email=email,
        password=password,
        output_dir=Path(args.output).expanduser(),
        headless=args.headless,
        novideo=args.novideo,
        nophotos=args.nophotos,
        fmt=args.format,
        keep_old=args.keep_old,
        smoothing=args.smoothing,
    ))


if __name__ == "__main__":
    main()
