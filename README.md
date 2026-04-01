# stealthcam-downloader

Downloads all photos from your [Stealth Cam Command](https://stealthcamcommand.com) trail camera gallery and assembles them into a timelapse video.

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
| `--output PATH` | `./trailcam_photos` | Destination folder |
| `--headless` | off | Run browser without a visible window |
| `--email EMAIL` | (prompt) | Account email |
| `--password PASS` | (prompt) | Account password |
| `--novideo` | off | Skip video creation |
| `--format FORMAT` | `mp4` | Video format: `mp4`, `webm`, or `gif` |
| `--keep-old` | off | Archive existing video with a timestamp instead of overwriting |
| `--optical-flow` | off | Interpolate 3 synthetic frames between each shot within a burst using optical flow |

### Example

```bash
python3 download_trailcam.py --output ~/TrailCam --headless --format mp4 --keep-old
```

## Example output

[Example timelapse (MP4)](https://files.catbox.moe/2lfxdm.webm)

## Output

```
trailcam_photos/
├── photos/          # full-resolution photos, named by capture order
│   ├── 0001_*.JPG
│   └── ...
├── trailcam.mp4     # timelapse at 3 fps, max 1280px wide
└── _api_responses.json
```

Re-running is safe: already-downloaded photos are skipped, and the video is rebuilt from everything in the photos folder.
