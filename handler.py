#!/usr/bin/env python3
"""
RunPod Serverless Handler — FFmpeg Video Encoder
=================================================
Offloads MP4 encoding (480p / 720p) from the AddisStream server to RunPod.

Input payload:
    {
        "input": {
            "input_r2_key":   "upload/videos/2026/02/movie.mp4",
            "output_prefix":  "upload/videos/2026/02/movie",
            "profiles": [
                {"tag": "480p", "height": 480, "crf": 26, "preset": "faster",
                 "profile": "main", "level": "3.1", "audio_br": "128k"},
                {"tag": "720p", "height": 720, "crf": 26, "preset": "faster",
                 "profile": "main", "level": "3.1", "audio_br": "128k"}
            ],
            "r2": {
                "account_id":  "...",
                "access_key":  "...",
                "secret_key":  "...",
                "bucket":      "ecinmax",
                "cdn_base":    "https://pub-xxx.r2.dev/"
            }
        }
    }

Output:
    {
        "outputs": {
            "480p": {"r2_key": "..._480p_converted.mp4", "url": "https://..."},
            "720p": {"r2_key": "..._720p_converted.mp4", "url": "https://..."}
        },
        "skipped": ["720p"],   # profiles skipped (e.g. upscale guard)
        "source_height": 1080
    }
"""

import runpod
import os
import json
import time
import subprocess
import tempfile
import boto3
from botocore.client import Config

FFMPEG  = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"


# ── R2 helpers ─────────────────────────────────────────────

def _s3(r2_cfg):
    return boto3.client(
        "s3",
        endpoint_url=f"https://{r2_cfg['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=r2_cfg["access_key"],
        aws_secret_access_key=r2_cfg["secret_key"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def download_from_r2(r2_cfg, r2_key, local_path):
    s3 = _s3(r2_cfg)
    s3.download_file(r2_cfg["bucket"], r2_key, local_path)


def upload_to_r2(r2_cfg, local_path, r2_key):
    s3 = _s3(r2_cfg)
    s3.upload_file(local_path, r2_cfg["bucket"], r2_key,
                   ExtraArgs={"ContentType": "video/mp4"})


# ── ffprobe ────────────────────────────────────────────────

def get_video_height(filepath):
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams", filepath],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(r.stdout)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                return int(s.get("height", 0))
    except Exception:
        pass
    return 0


# ── ffmpeg encode ──────────────────────────────────────────

def encode_mp4(input_path, output_path, profile):
    height = profile["height"]
    cmd = [
        FFMPEG, "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-profile:v", profile.get("profile", "main"),
        "-level", profile.get("level", "3.1"),
        "-preset", profile.get("preset", "faster"),
        "-crf", str(profile.get("crf", 26)),
        "-vf", f"scale=-2:{height}",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", profile.get("audio_br", "128k"),
        "-movflags", "+faststart",
        output_path,
    ]
    start = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    elapsed = int(time.time() - start)

    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed ({profile['tag']}) exit={proc.returncode} "
            f"after {elapsed}s: {proc.stderr[-400:]}"
        )
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
        raise RuntimeError(f"ffmpeg output missing or empty for {profile['tag']}")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"[{profile['tag']}] encoded in {elapsed}s — {size_mb:.1f} MB")
    return elapsed


# ── RunPod handler ─────────────────────────────────────────

def handler(job):
    inp       = job["input"]
    r2_cfg    = inp["r2"]
    r2_key    = inp["input_r2_key"]
    prefix    = inp["output_prefix"]
    profiles  = inp.get("profiles", [
        {"tag": "480p", "height": 480, "crf": 26, "preset": "faster",
         "profile": "main", "level": "3.1", "audio_br": "128k"},
        {"tag": "720p", "height": 720, "crf": 26, "preset": "faster",
         "profile": "main", "level": "3.1", "audio_br": "128k"},
    ])

    with tempfile.TemporaryDirectory() as tmp:
        # 1. Download source from R2
        ext = os.path.splitext(r2_key)[1] or ".mp4"
        source = os.path.join(tmp, f"source{ext}")
        print(f"Downloading {r2_key} ...")
        download_from_r2(r2_cfg, r2_key, source)
        src_size = os.path.getsize(source) / (1024 * 1024)
        print(f"Downloaded: {src_size:.0f} MB")

        # 2. Probe source height
        src_height = get_video_height(source)
        print(f"Source height: {src_height}p")

        outputs = {}
        skipped = []

        for profile in profiles:
            tag    = profile["tag"]
            height = profile["height"]

            # Upscale guard
            if src_height > 0 and height > src_height + 50:
                print(f"[{tag}] Skipping — would upscale {src_height}p → {height}p")
                skipped.append(tag)
                continue

            out_filename = f"{os.path.basename(prefix)}_{tag}_converted.mp4"
            out_local    = os.path.join(tmp, out_filename)
            out_r2_key   = f"{os.path.dirname(prefix)}/{out_filename}".lstrip("/")

            encode_mp4(source, out_local, profile)

            print(f"[{tag}] Uploading to R2: {out_r2_key}")
            upload_to_r2(r2_cfg, out_local, out_r2_key)

            cdn_base = r2_cfg.get("cdn_base", "").rstrip("/")
            outputs[tag] = {
                "r2_key": out_r2_key,
                "url":    f"{cdn_base}/{out_r2_key}" if cdn_base else "",
            }

        if not outputs:
            raise RuntimeError("No profiles were encoded (all skipped or failed)")

        return {
            "outputs":       outputs,
            "skipped":       skipped,
            "source_height": src_height,
        }


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
