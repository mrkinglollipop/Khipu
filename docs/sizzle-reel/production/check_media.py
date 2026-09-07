#!/usr/bin/env python3
"""Validate exported media and record checksums for portable reel assets."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"


def command(args: list[str], timeout: int = 600) -> bytes:
    return subprocess.check_output(args, timeout=timeout)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audio_hash(path: Path) -> str:
    return command(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
                    "-c", "copy", "-f", "hash", "-hash", "sha256", "-"]).decode().strip()


def probe(path: Path) -> dict:
    return json.loads(command(["ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,nb_frames",
        "-of", "json", str(path)]))


def frame_count(path: Path, start: int, seconds: int) -> tuple[int, int]:
    raw = command(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path), "-t", str(seconds),
                   "-vf", "scale=96:96", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
    size = 96 * 96 * 3
    frames = [raw[offset:offset + size] for offset in range(0, len(raw), size)]
    return len(frames), len(set(frames))


def write_manifest() -> None:
    entries = []
    for path in sorted([*ASSETS.rglob("*"), *(ROOT / "fonts").rglob("*")]):
        if path.is_file():
            entries.append({"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path), "size": path.stat().st_size})
    (ROOT / "assets.manifest.json").write_text(json.dumps({"version": 1, "assets": entries}, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-manifest", action="store_true",
                        help="Record intentionally changed source assets before validation")
    args = parser.parse_args()
    if args.refresh_manifest:
        write_manifest()
    else:
        manifest = json.loads((ROOT / "assets.manifest.json").read_text())
        for entry in manifest["assets"]:
            path = ROOT / entry["path"]
            assert path.is_file(), f"Missing release asset: {entry['path']}"
            assert path.stat().st_size == entry["size"], f"Asset size differs: {entry['path']}"
            assert sha256(path) == entry["sha256"], f"Asset checksum differs: {entry['path']}"
    report = {}
    accepted_audio = audio_hash(ASSETS / "music" / "approved-music.m4a")
    for shape, height in (("square", 1080), ("vertical", 1920)):
        output = ROOT / "exports" / f"khipu-ad-{shape}.mp4"
        details = probe(output)
        video = next(stream for stream in details["streams"] if stream["codec_type"] == "video")
        assert video["codec_name"] == "h264"
        assert (video["width"], video["height"]) == (1080, height)
        assert video["r_frame_rate"] == "24/1" and video["nb_frames"] == "600"
        assert float(details["format"]["duration"]) == 25
        assert audio_hash(output) == accepted_audio
        connection_frames, unique_frames = frame_count(output, 13, 3)
        assert (connection_frames, unique_frames) == (72, 72)
        command(["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"])
        report[shape] = {"decode_exit": 0, "duration_seconds": 25, "frames": 600,
                         "resolution": [1080, height], "audio_sha256": accepted_audio,
                         "connection_frames": connection_frames, "unique_connection_frames": unique_frames}
    (ROOT / "media-checks.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Both exports: decoded, 600 frames, approved AAC preserved, 72 unique connection frames.")
