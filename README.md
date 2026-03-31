# stealthcam-downloader

Downloads all photos from your [Stealth Cam Command](https://stealthcamcommand.com) trail camera gallery and assembles them into a WebM timelapse.

## Requirements

Python 3.10+ and ffmpeg:

```bash
pip install playwright httpx pillow
playwright install chromium
```

ffmpeg must be on your PATH (`sudo pacman -S ffmpeg` / `sudo apt install ffmpeg` / `brew install ffmpeg`).

## Usage

```bash
python3 download_trailcam.py
```

You'll be prompted for your Stealth Cam Command email and password.

### Options

| Flag | Default | Description |
|---|---|---|
| `--output PATH` | `./trailcam_photos` | Folder to save photos and video |
| `--headless` | off | Run browser without a visible window |
| `--email EMAIL` | (prompt) | Account email |
| `--password PASS` | (prompt) | Account password |

### Example

```bash
python3 download_trailcam.py --output ~/TrailCam --headless
```

## Example output

<video src="example.webm" controls width="100%"></video>

## Output

- `trailcam_photos/*.JPG` — full-resolution photos, named by capture order
- `trailcam_photos/trailcam.webm` — VP9 WebM timelapse at 3 fps, max 1280px wide
- `trailcam_photos/_api_responses.json` — raw API responses (useful for debugging)

Re-running is safe: already-downloaded photos are skipped, and the WebM is rebuilt from everything in the folder.
