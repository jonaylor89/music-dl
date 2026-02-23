# music-dl

Download music from [Suno](https://suno.com) or other gen ai platforms as audio files.

> **⚠️ For educational and personal use only.** This tool is intended for downloading your own creations. Respect copyright and DRM laws in your jurisdiction.

## Quick Start

```bash
# Install dependencies
uv sync

# Download a song
uv run python music_dl.py 'https://suno.com/song/...'
uv run python music_dl.py 'https://u---.com/song/...'
```

## DRM Songs

Some songs use Widevine DRM streaming. You'll need a `.wvd` device file to decrypt them.

### Option 1: Extract from Android emulator (automated)

```bash
# Prerequisites: adb + Java 11+
#   Linux:   sudo pacman -S android-tools
#   macOS:   brew install android-platform-tools
#   Windows: choco install adb

uv run python setup_cdm.py
```

This boots a headless Android emulator, extracts an L3 CDM, and saves it to `~/.config/music-dl/device.wvd`. Then just run `music_dl.py` as normal.

### Option 2: Provide a key manually

```bash
uv run python music_dl.py --key <32-char-hex> '<song-url>'
```

## Usage

```
music-dl [-h] [-o OUTPUT] [-c FILE] [-k KEY] url

  url              Song URL
  -o, --output     Output directory (default: current)
  -c, --cdm        Path to .wvd device file
  -k, --key        Decryption key (32-char hex)
```

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- [ffmpeg](https://ffmpeg.org/) (for DRM decryption)

## Disclaimer

This project is for **educational and research purposes only**. It is designed to help users access audio from their own content. Do not use this tool to circumvent DRM on content you do not own. The authors are not responsible for any misuse.
