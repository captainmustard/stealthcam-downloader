# stealthcam-downloader

Downloads all photos from your [Stealth Cam Command](https://stealthcamcommand.com) trail camera gallery and assembles them into a timelapse video.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — manages Python and dependencies automatically
- ffmpeg — `sudo pacman -S ffmpeg` / `sudo apt install ffmpeg` / `brew install ffmpeg`

## Setup

```bash
uv sync
```

This creates a `.venv` in the project directory and installs all dependencies. The Chromium browser is installed automatically on first run.

## Usage

```bash
uv run download_trailcam.py
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
| `--nophotos` | off | Skip downloading photos (useful for rebuilding the video from existing photos) |
| `--format FORMAT` | `mp4` | Video format: `mp4`, `webm`, or `gif` |
| `--keep-old` | off | Archive existing video with a timestamp instead of overwriting |
| `--smoothing` | off | Crossfade between shots within each burst, holding each photo for 0.5s before transitioning |

### Example

```bash
uv run download_trailcam.py --output ~/TrailCam --headless --format mp4 --keep-old
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
