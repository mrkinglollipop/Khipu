#!/usr/bin/env python3
"""Render the approved Khipu sizzle reel from portable local assets.

The default path joins the approved segment plates losslessly.  Use
--rebuild-connection only when intentionally regenerating the editable
three-second connection plate from its retained source footage.
"""
import argparse
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
FONTS = ROOT / "fonts"
OUTPUTS = ROOT / "exports"
WORK = ROOT / "build"
FPS = 24
PAPER = "0xf4ebe3"
INK = "0x22242b"
RUST = "0xc45c3e"


def run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], cwd=ROOT,
                   check=True, timeout=600)


def caption(path: Path, text: str, font_name: str, size: int, color: str) -> None:
    font = ImageFont.truetype(str(FONTS / font_name), size)
    box = font.getbbox(text)
    image = Image.new("RGBA", (1080, box[3] - box[1] + 8))
    ImageDraw.Draw(image).text(((1080 - font.getlength(text)) / 2, 4 - box[1]), text,
                               font=font, fill="#" + color[2:])
    image.save(path)


def rebuild_connection(shape: str, height: int) -> Path:
    """Recreate the approved connection plate from retained source footage."""
    WORK.mkdir(exist_ok=True)
    first = WORK / f"{shape}-pick-up.png"
    second = WORK / f"{shape}-thread.png"
    size = 96 if height == 1920 else 86
    y1, y2 = ((330, 435) if height == 1920 else (48, 143))
    caption(first, "Pick up", "fraunces-bold.ttf", size, RUST)
    caption(second, "the thread.", "fraunces-bold.ttf", size, INK)
    alpha = "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*min(clip(Y/160,0,1),clip((H-Y)/100,0,1))'"
    filters = [
        "[0:v]split[bgclose][bgwide]",
        "[bgclose]trim=duration=2,setpts=PTS-STARTPTS[closepaper]",
        "[bgwide]trim=duration=1,setpts=PTS-STARTPTS[widepaper]",
        f"[1:v]trim=start=0:end=2,setpts=PTS-STARTPTS,scale=1080:1080:flags=lanczos,format=rgba,{alpha}[plate]",
        f"[2:v]trim=start=2:end=3,setpts=PTS-STARTPTS,scale=1080:1080:flags=lanczos,format=rgba,{alpha}[reveal]",
        f"[closepaper][plate]overlay=0:{420 if height == 1920 else 0}:shortest=1[close]",
        f"[widepaper][reveal]overlay=0:{580 if height == 1920 else 160}:shortest=1[wide]",
        "[close][wide]concat=n=2:v=1:a=0[scene]",
        "[3:v]format=rgba,fade=t=in:st=0:d=0.125:alpha=1[one]",
        "[4:v]format=rgba,fade=t=in:st=0:d=0.125:alpha=1[two]",
        f"[scene][one]overlay=0:'{y1}+12*(1-clip(t/0.125,0,1))':enable='lt(t,3)'[withone]",
        f"[withone][two]overlay=0:'{y2}+12*(1-clip(t/0.125,0,1))':enable='lt(t,3)',format=yuv420p[v]",
    ]
    plate = WORK / f"{shape}-connection.mp4"
    run(["-f", "lavfi", "-i", f"color=c={PAPER}:s=1080x{height}:r=24:d=3",
         "-i", str(ASSETS / "connection" / "connection-source.mp4"),
         "-i", str(ASSETS / "connection" / "wide-reveal-source.mp4"),
         "-loop", "1", "-framerate", "24", "-i", str(first),
         "-loop", "1", "-framerate", "24", "-i", str(second),
         "-filter_complex", ";".join(filters), "-map", "[v]", "-an", "-t", "3",
         "-r", "24", "-c:v", "libx264", "-preset", "medium", "-crf", "16", str(plate)])
    return plate


def build(shape: str, height: int, rebuild: bool) -> None:
    connection = rebuild_connection(shape, height) if rebuild else ASSETS / "segments" / f"{shape}-connection.mp4"
    files = [ASSETS / "segments" / f"{shape}-first13.mp4", connection,
             ASSETS / "segments" / f"{shape}-card.mp4", ASSETS / "segments" / f"{shape}-outro.mp4"]
    WORK.mkdir(exist_ok=True)
    listing = WORK / f"{shape}-concat.txt"
    listing.write_text("".join(f"file '{path}'\n" for path in files))
    output = OUTPUTS / f"khipu-ad-{shape}.mp4"
    run(["-f", "concat", "-safe", "0", "-i", str(listing),
         "-i", str(ASSETS / "music" / "approved-music.m4a"), "-map", "0:v:0", "-map", "1:a:0",
         "-c", "copy", "-t", "25", "-movflags", "+faststart", str(output)])
    run(["-xerror", "-i", str(output), "-f", "null", "-"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild-connection", action="store_true")
    args = parser.parse_args()
    OUTPUTS.mkdir(exist_ok=True)
    build("square", 1080, args.rebuild_connection)
    build("vertical", 1920, args.rebuild_connection)
