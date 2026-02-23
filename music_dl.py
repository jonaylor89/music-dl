#!/usr/bin/env python3
"""Download music from Suno or other music platforms."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

import requests
from tqdm import tqdm

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
)


def detect_platform(url: str) -> str:
    """Return the platform name based on the URL."""
    if "suno.com" in url:
        return "suno"
    return "other"


# ---------------------------------------------------------------------------
# Song metadata extraction
# ---------------------------------------------------------------------------


def fetch_page(url: str) -> str:
    resp = SESSION.get(url)
    resp.raise_for_status()
    return resp.text


def extract_song_info(html: str) -> dict:
    """Extract song metadata from the Next.js page HTML."""
    # Gather all __next_f payloads
    payload = ""
    for m in re.finditer(
        r'self\.__next_f\.push\(\s*\[.*?,\s*"((?:[^"\\]|\\.)*)"\s*\]\s*\)', html
    ):
        raw = m.group(1)
        raw = raw.replace('\\"', '"').replace("\\\\", "\\")
        raw = raw.replace("\\/", "/").replace("\\n", "\n").replace("\\t", "\t")
        payload += raw

    info = _find_song_in_text(payload) or _find_song_in_text(html)
    if not info:
        raise RuntimeError("Could not find song metadata in page")
    return info


def _find_song_in_text(text: str) -> dict | None:
    m = re.search(r'"song_path"\s*:\s*"([^"]+)"', text)
    if not m:
        return None
    song_path = unquote(unquote(m.group(1)))

    pos = m.start()
    ctx = text[max(0, pos - 2000) : pos + 500]

    def _json_str(key: str) -> str:
        m2 = re.search(rf'"{key}"\s*:\s*"([^"]*)"', ctx)
        return m2.group(1) if m2 else "Unknown"

    return {
        "id": _json_str("id"),
        "title": _json_str("title"),
        "artist": _json_str("artist"),
        "song_path": song_path,
    }


# ---------------------------------------------------------------------------
# Suno metadata extraction
# ---------------------------------------------------------------------------


def extract_suno_info(html: str, url: str) -> dict:
    """Extract song metadata from Suno page OG tags."""
    m = re.search(r'property="og:title"\s+content="([^"]*)"', html)
    title = m.group(1) if m else "Unknown"

    m = re.search(r'property="og:audio"\s+content="([^"]*)"', html)
    audio_url = m.group(1) if m else None

    # description format: "Title by Artist (@handle). ..."
    artist = "Unknown"
    m = re.search(r'name="description"\s+content="([^"]*)"', html)
    if m:
        desc = m.group(1)
        m2 = re.search(r" by (.+?)(?:\s*\(@.+?\))?\.?\s*(?:Listen|$)", desc)
        if m2:
            artist = m2.group(1).strip()

    # Extract song ID from URL
    m = re.search(r"/song/([a-f0-9-]+)", url)
    song_id = m.group(1) if m else "Unknown"

    if not audio_url:
        audio_url = f"https://cdn1.suno.ai/{song_id}.mp3"

    return {
        "id": song_id,
        "title": title,
        "artist": artist,
        "song_path": audio_url,
    }


def safe_filename(artist: str, title: str, ext: str) -> str:
    name = f"{artist} - {title}.{ext}"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


# ---------------------------------------------------------------------------
# Direct MP3 download
# ---------------------------------------------------------------------------


def try_download_mp3(song_path: str, dest: Path) -> bool:
    """Try the direct MP3 link. Returns True if successful."""
    resp = SESSION.get(song_path, stream=True)
    if resp.status_code in (403, 404):
        return False
    resp.raise_for_status()

    total = int(resp.headers.get("Content-Length", 0)) or None
    part = dest.with_suffix(".mp3.part")
    with open(part, "wb") as f, tqdm(
        total=total, unit="B", unit_scale=True, desc="Downloading MP3"
    ) as pbar:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            f.write(chunk)
            pbar.update(len(chunk))

    part.rename(dest)
    return True


# ---------------------------------------------------------------------------
# HLS manifest parsing
# ---------------------------------------------------------------------------


def fetch_stream_segments(song_id: str) -> dict:
    url = f"https://stream.udio.com/api/v2/audio-stream/content/{song_id}/manifest.m3u8"
    resp = SESSION.get(url, headers={"Origin": "https://www.udio.com"})
    resp.raise_for_status()
    manifest = resp.text

    m = re.search(r"KEYID=0x([0-9a-fA-F]+)", manifest)
    key_id = m.group(1).lower() if m else song_id.replace("-", "")

    m = re.search(r'URI="data:text/plain;base64,([A-Za-z0-9+/=]+)"', manifest)
    pssh_b64 = m.group(1) if m else None

    m = re.search(r'EXT-X-MAP:URI="([^"]+)"', manifest)
    init_uri = m.group(1) if m else f"/api/v2/audio-stream/content/{song_id}/init.mp4"

    segment_uris = []
    after_extinf = False
    for line in manifest.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            after_extinf = True
        elif after_extinf and line and not line.startswith("#"):
            segment_uris.append(line)
            after_extinf = False

    if not segment_uris:
        raise RuntimeError("No media segments found in HLS manifest")

    return {
        "key_id": key_id,
        "pssh_b64": pssh_b64,
        "init_uri": init_uri,
        "segment_uris": segment_uris,
    }


def resolve_stream_uri(uri: str) -> str:
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    if uri.startswith("/"):
        return f"https://stream.udio.com{uri}"
    return f"https://stream.udio.com/{uri}"


# ---------------------------------------------------------------------------
# Widevine key acquisition
# ---------------------------------------------------------------------------

LICENSE_URL = "https://stream.udio.com/drm/license?type=widevine"


def _config_dir() -> Path:
    """Platform-appropriate config directory."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "music-dl"
    return Path.home() / ".config" / "music-dl"


def find_wvd_device() -> Path | None:
    """Search common locations for a .wvd device file."""
    env = os.environ.get("MUSIC_DL_CDM")
    if env:
        p = Path(env)
        if p.exists():
            return p

    config_dir = _config_dir()
    wvd = config_dir / "device.wvd"
    if wvd.exists():
        return wvd

    if config_dir.is_dir():
        for f in config_dir.iterdir():
            if f.suffix == ".wvd":
                return f

    return None


def obtain_content_key(cdm_path: Path, pssh_b64: str, key_id: str) -> str:
    """Get the content decryption key using pywidevine."""
    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    from pywidevine.pssh import PSSH

    device = Device.load(cdm_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    pssh = PSSH(pssh_b64)
    challenge = cdm.get_license_challenge(session_id, pssh)

    resp = SESSION.post(
        LICENSE_URL,
        data=challenge,
        headers={"Origin": "https://www.udio.com"},
    )
    resp.raise_for_status()

    cdm.parse_license(session_id, resp.content)

    content_key = None
    for key in cdm.get_keys(session_id):
        if str(key.type) == "CONTENT":
            kid_hex = key.kid.hex
            key_hex = key.key.hex()
            if kid_hex == key_id:
                cdm.close(session_id)
                return key_hex
            if content_key is None:
                content_key = key_hex

    cdm.close(session_id)
    if content_key:
        return content_key
    raise RuntimeError("No content key found in license response")


# ---------------------------------------------------------------------------
# DRM stream download + decrypt
# ---------------------------------------------------------------------------


def download_drm_stream(song_id: str, key_hex: str, dest: Path) -> None:
    key_hex = key_hex.strip().lower()
    if len(key_hex) != 32 or not all(c in "0123456789abcdef" for c in key_hex):
        raise ValueError("Decryption key must be exactly 32 hex characters")

    print("Fetching HLS manifest...", file=sys.stderr)
    segs = fetch_stream_segments(song_id)
    print(f"  Key ID:   {segs['key_id']}", file=sys.stderr)
    print(f"  Segments: {len(segs['segment_uris'])} + init", file=sys.stderr)

    # Download encrypted segments into a temp file
    with tempfile.NamedTemporaryFile(suffix=".enc.m4a", delete=False) as tmp_enc:
        enc_path = Path(tmp_enc.name)

        # Init segment
        init_url = resolve_stream_uri(segs["init_uri"])
        init_data = SESSION.get(
            init_url, headers={"Origin": "https://www.udio.com"}
        ).content
        tmp_enc.write(init_data)

        # Media segments
        for seg_name in tqdm(segs["segment_uris"], desc="Downloading segments"):
            seg_url = resolve_stream_uri(seg_name)
            resp = SESSION.get(seg_url, headers={"Origin": "https://www.udio.com"})
            resp.raise_for_status()
            tmp_enc.write(resp.content)

    try:
        # Decrypt with ffmpeg
        print("Decrypting with ffmpeg...", file=sys.stderr)
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel", "error",
                "-decryption_key", key_hex,
                "-i", str(enc_path),
                "-c", "copy",
                str(dest),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg decryption failed: {result.stderr}")
    finally:
        enc_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        prog="music-dl",
        description="Download music from Suno or other music platforms",
    )
    parser.add_argument(
        "url",
        help="Song URL (e.g. https://suno.com/song/xxxxx)",
    )
    parser.add_argument("-o", "--output", default=".", help="Output directory")
    parser.add_argument(
        "-c",
        "--cdm",
        metavar="FILE",
        help="Widevine device file (.wvd). Auto-detected from "
        "~/.config/music-dl/ or $MUSIC_DL_CDM if not specified.",
    )
    parser.add_argument(
        "-k",
        "--key",
        help="Decryption key (32-char hex). Skips automatic key acquisition.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    platform = detect_platform(args.url)

    print("Fetching song page...", file=sys.stderr)
    html = fetch_page(args.url)

    if platform == "suno":
        info = extract_suno_info(html, args.url)
    else:
        info = extract_song_info(html)

    print(f"  Title:  {info['title']}", file=sys.stderr)
    print(f"  Artist: {info['artist']}", file=sys.stderr)

    # Try direct MP3 download first
    mp3_name = safe_filename(info["artist"], info["title"], "mp3")
    mp3_dest = output_dir / mp3_name

    print(f"Downloading: {mp3_name}", file=sys.stderr)
    try:
        if try_download_mp3(info["song_path"], mp3_dest):
            print(f"Saved to: {mp3_dest}", file=sys.stderr)
            return
        print("Direct MP3 not available (song uses DRM streaming).", file=sys.stderr)
    except Exception as e:
        print(f"Direct MP3 failed: {e}", file=sys.stderr)

    if platform == "suno":
        print("Failed to download from Suno.", file=sys.stderr)
        sys.exit(1)

    # Fall back to DRM stream
    song_id = info["id"]
    if not song_id or song_id == "Unknown":
        print("Could not determine song UUID for stream URL", file=sys.stderr)
        sys.exit(1)

    # Determine decryption key
    if args.key:
        key_hex = args.key
    else:
        print("Fetching stream info...", file=sys.stderr)
        segs = fetch_stream_segments(song_id)

        cdm_path = Path(args.cdm) if args.cdm else find_wvd_device()

        if cdm_path:
            if not cdm_path.exists():
                print(f"CDM device file not found: {cdm_path}", file=sys.stderr)
                sys.exit(1)
            pssh = segs.get("pssh_b64")
            if not pssh:
                print("No PSSH found in manifest", file=sys.stderr)
                sys.exit(1)
            print(f"Using CDM: {cdm_path}", file=sys.stderr)
            print("Acquiring content key...", file=sys.stderr)
            key_hex = obtain_content_key(cdm_path, pssh, segs["key_id"])
        else:
            print(f"  Key ID:       {segs['key_id']}", file=sys.stderr)
            if segs.get("pssh_b64"):
                print(f"  PSSH (b64):   {segs['pssh_b64']}", file=sys.stderr)
            print(f"  License URL:  {LICENSE_URL}", file=sys.stderr)
            print(file=sys.stderr)
            print(
                "This song requires DRM decryption. Provide either:\n"
                "  --cdm <device.wvd>  (automatic, needs: pip install pywidevine)\n"
                "  --key <hex>         (manual, 32-char hex content key)\n\n"
                "To skip --cdm every time, place your .wvd file at:\n"
                f"  {_config_dir() / 'device.wvd'}\n"
                "  or set MUSIC_DL_CDM=/path/to/device.wvd\n\n"
                "To extract a CDM from an Android emulator, run:\n"
                "  uv run python setup_cdm.py",
                file=sys.stderr,
            )
            sys.exit(1)

    m4a_name = safe_filename(info["artist"], info["title"], "m4a")
    m4a_dest = output_dir / m4a_name
    print(f"Downloading DRM stream: {m4a_name}", file=sys.stderr)
    download_drm_stream(song_id, key_hex, m4a_dest)

    print(f"Saved to: {m4a_dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
