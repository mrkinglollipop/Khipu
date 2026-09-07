# Khipu sizzle reel production package

This package renders the approved 25-second square and vertical Khipu reel from portable local media. It contains the accepted video plates, exact approved AAC music, editable connection source footage, reference still, prompts, and OFL fonts. It makes no network or paid API calls.

Fetch the media archive from the dedicated non-latest media release before rendering. The
approved final MP4s are published there too; they remain the release masters even if a
future local encode differs in container metadata.

```sh
cd docs/sizzle-reel/production
curl -L https://github.com/mrkinglollipop/Khipu/releases/download/khipu-social-reel-2026-09-07/khipu-reel-source-assets.zip -o khipu-reel-source-assets.zip
unzip khipu-reel-source-assets.zip
python3 -m pip install -r requirements.txt
python3 render.py
python3 check_media.py
```

Requires Python 3.10 or newer plus `ffmpeg` and `ffprobe` on PATH. The release was checked with FFmpeg 8.1.2. `render.py` writes `exports/khipu-ad-square.mp4` and `exports/khipu-ad-vertical.mp4`. `check_media.py` verifies every source asset against the committed checksum manifest, fully decodes both files, and checks dimensions, cadence, exact AAC stream, and the 72 unique frames in the connection scene. After intentionally changing source media, use `check_media.py --refresh-manifest` to record the new asset checksums.

The normal render losslessly joins the approved plates. To deliberately recreate the editable connection plate from `assets/connection/`, run:

```sh
python3 render.py --rebuild-connection
python3 check_media.py
```

The rebuild is for controlled changes to that scene; use the default render to reproduce the approved picture. The opening, knotting montage, agent card, and outro are retained as approved rendered plates, with their type and motion baked in. The connection scene has its original footage and editable caption/compositing code. All media paths are package-relative; no API key is needed.
