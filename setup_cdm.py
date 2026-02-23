#!/usr/bin/env python3
"""
Cross-platform setup script to extract a Widevine L3 CDM from an Android emulator.

Supports Linux (x86_64), macOS (Intel & Apple Silicon), and Windows (x86_64).

Prerequisites:
  - Python 3.10+
  - adb (Android Debug Bridge) on PATH
      Linux:   sudo pacman -S android-tools  (or apt install android-tools-adb)
      macOS:   brew install android-platform-tools
      Windows: choco install adb  (or install Android Studio)
  - Java 11+ (for sdkmanager/avdmanager)

Usage:
  uv run python setup_cdm.py
"""

import io
import json
import lzma
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request

# ── Configuration ────────────────────────────────────────────────────

FRIDA_VERSION = None  # auto-detected from installed keydive's frida dependency
API_LEVEL = 34
AVD_NAME = "widevine_cdm"

# ── Platform detection ───────────────────────────────────────────────

SYSTEM = platform.system().lower()  # linux, darwin, windows
MACHINE = platform.machine().lower()  # x86_64, amd64, arm64, aarch64

# Map to Android emulator arch and SDK naming
if MACHINE in ("x86_64", "amd64"):
    EMU_ARCH = "x86_64"
    FRIDA_ARCH = "x86_64"
elif MACHINE in ("arm64", "aarch64"):
    EMU_ARCH = "arm64-v8a"
    FRIDA_ARCH = "arm64"
else:
    print(f"Unsupported architecture: {MACHINE}", file=sys.stderr)
    sys.exit(1)

# SDK command-line tools download URLs per platform
SDK_TOOLS_URLS = {
    "linux": "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip",
    "darwin": "https://dl.google.com/android/repository/commandlinetools-mac-11076708_latest.zip",
    "windows": "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip",
}

# SDK / config paths
if SYSTEM == "windows":
    SDK_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Android" / "Sdk"
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "music-dl"
else:
    SDK_DIR = Path.home() / "Android" / "Sdk"
    CONFIG_DIR = Path.home() / ".config" / "music-dl"


# ── Helpers ──────────────────────────────────────────────────────────

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"

if SYSTEM == "windows":
    os.system("")  # enable ANSI on Windows


def info(msg: str):
    print(f"{GREEN}[+]{RESET} {msg}")


def warn(msg: str):
    print(f"{YELLOW}[!]{RESET} {msg}")


def err(msg: str):
    print(f"{RED}[!]{RESET} {msg}", file=sys.stderr)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


def download(url: str, desc: str = "") -> bytes:
    req = Request(url, headers={"User-Agent": "music-dl-setup/1.0"})
    info(f"Downloading {desc or url}...")
    with urlopen(req) as resp:
        return resp.read()


def _avd_home() -> Path:
    """Locate the AVD directory, respecting XDG_CONFIG_HOME if set."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        avd_dir = Path(xdg) / ".android" / "avd"
        if avd_dir.exists():
            return avd_dir
    return Path.home() / ".android" / "avd"


def sdk_env() -> dict[str, str]:
    """Build environment dict with Android SDK paths set correctly."""
    env = os.environ.copy()
    env["ANDROID_HOME"] = str(SDK_DIR)
    env["ANDROID_SDK_ROOT"] = str(SDK_DIR)
    env["ANDROID_AVD_HOME"] = str(_avd_home())
    env["PATH"] = os.pathsep.join([
        str(SDK_DIR / "cmdline-tools" / "latest" / "bin"),
        str(SDK_DIR / "emulator"),
        str(SDK_DIR / "platform-tools"),
        env.get("PATH", ""),
    ])
    return env


def which(name: str) -> str | None:
    return shutil.which(name)


def sdk_bin(name: str) -> str:
    """Resolve an SDK binary, adding .bat on Windows."""
    base = SDK_DIR / "cmdline-tools" / "latest" / "bin" / name
    if SYSTEM == "windows":
        bat = base.with_suffix(".bat")
        if bat.exists():
            return str(bat)
    return str(base)


def emulator_bin() -> str:
    emu = SDK_DIR / "emulator" / ("emulator.exe" if SYSTEM == "windows" else "emulator")
    return str(emu)


# ── Step 0: Check prerequisites ─────────────────────────────────────

def check_prerequisites():
    if not which("adb"):
        err("adb not found on PATH.")
        if SYSTEM == "linux":
            err("  Install with: sudo pacman -S android-tools  (Arch)")
            err("            or: sudo apt install android-tools-adb  (Debian/Ubuntu)")
        elif SYSTEM == "darwin":
            err("  Install with: brew install android-platform-tools")
        else:
            err("  Install with: choco install adb  (or install Android Studio)")
        sys.exit(1)

    # Check hardware acceleration
    if SYSTEM == "linux":
        kvm = Path("/dev/kvm")
        if not kvm.exists():
            err("/dev/kvm not found. Enable KVM in your BIOS/kernel.")
            sys.exit(1)
        if not os.access(kvm, os.W_OK):
            err("/dev/kvm not writable. Run: sudo chmod 666 /dev/kvm")
            sys.exit(1)
    elif SYSTEM == "darwin":
        # macOS uses Hypervisor.framework, no special check needed
        pass
    else:
        # Windows uses HAXM or Hyper-V — the emulator will error if unavailable
        pass


# ── Step 1: Install Android SDK command-line tools ───────────────────

def install_sdk_tools():
    sdkmanager = sdk_bin("sdkmanager")
    if Path(sdkmanager).exists():
        info("SDK command-line tools already installed")
        return

    tools_url = SDK_TOOLS_URLS.get(SYSTEM)
    if not tools_url:
        err(f"No SDK tools URL for platform: {SYSTEM}")
        sys.exit(1)

    data = download(tools_url, "Android SDK command-line tools")
    dest = SDK_DIR / "cmdline-tools"
    dest.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest)

    # Google packages it as cmdline-tools/cmdline-tools/..., rename to latest
    extracted = dest / "cmdline-tools"
    target = dest / "latest"
    if extracted.exists():
        if target.exists():
            shutil.rmtree(target)
        extracted.rename(target)

    # Make binaries executable on Unix
    if SYSTEM != "windows":
        for f in (target / "bin").iterdir():
            f.chmod(f.stat().st_mode | 0o755)


# ── Step 2: Install emulator + system image ──────────────────────────

def install_emulator_image():
    info(f"Installing emulator and system image (API {API_LEVEL}, {EMU_ARCH})...")

    sdkmanager = sdk_bin("sdkmanager")
    env = sdk_env()

    # Accept licenses
    run([sdkmanager, "--licenses"], input=b"y\n" * 20, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    image = f"system-images;android-{API_LEVEL};google_apis;{EMU_ARCH}"
    run([sdkmanager, "emulator", "platform-tools",
         f"platforms;android-{API_LEVEL}", image], env=env)


# ── Step 3: Create AVD ───────────────────────────────────────────────

def create_avd():
    avdmanager = sdk_bin("avdmanager")
    env = sdk_env()

    result = run([avdmanager, "list", "avd"], capture_output=True, text=True, env=env)
    if AVD_NAME in result.stdout:
        info(f"AVD '{AVD_NAME}' already exists")
        return

    info(f"Creating AVD: {AVD_NAME}")
    image = f"system-images;android-{API_LEVEL};google_apis;{EMU_ARCH}"
    run([avdmanager, "create", "avd",
         "-n", AVD_NAME,
         "-k", image,
         "--device", "pixel_6",
         "--force"],
        input=b"no\n", env=env)


# ── Step 4: Boot emulator ────────────────────────────────────────────

def boot_emulator():
    result = run(["adb", "devices"], capture_output=True, text=True)
    if "emulator" in result.stdout:
        info("Emulator already running")
        return

    info("Starting emulator (headless)...")
    env = sdk_env()

    emu = emulator_bin()
    emu_cmd = [emu, "-avd", AVD_NAME, "-no-window", "-no-audio",
               "-gpu", "swiftshader_indirect", "-no-snapshot-load",
               "-writable-system"]

    log = open(tempfile.gettempdir() + "/emu.log", "w")
    subprocess.Popen(emu_cmd, stdout=log, stderr=log, env=env)

    info("Waiting for emulator to boot (1-3 minutes)...")
    run(["adb", "wait-for-device"])

    for _ in range(120):
        result = run(["adb", "shell", "getprop", "sys.boot_completed"],
                     capture_output=True, text=True)
        if result.stdout.strip() == "1":
            break
        time.sleep(2)
    else:
        err("Emulator did not boot in time. Check /tmp/emu.log")
        sys.exit(1)

    info("Emulator booted successfully")


# ── Step 5: Push frida-server ────────────────────────────────────────

def _detect_frida_version() -> str:
    """Get the frida version that keydive depends on (major must match server)."""
    global FRIDA_VERSION
    if FRIDA_VERSION:
        return FRIDA_VERSION
    result = run(
        ["uv", "tool", "run", "--from", "keydive",
         "python", "-c", "import frida; print(frida.__version__)"],
        capture_output=True, text=True,
    )
    ver = result.stdout.strip()
    if not ver:
        ver = "17.7.3"
        warn(f"Could not detect frida version, defaulting to {ver}")
    FRIDA_VERSION = ver
    return ver


def setup_frida():
    frida_ver = _detect_frida_version()
    info(f"Using frida-server {frida_ver} (matches keydive's frida)")

    info("Setting up root access...")
    run(["adb", "root"])
    time.sleep(2)

    archive_name = f"frida-server-{frida_ver}-android-{FRIDA_ARCH}.xz"
    frida_url = (
        f"https://github.com/frida/frida/releases/download/"
        f"{frida_ver}/{archive_name}"
    )

    tmp = Path(tempfile.gettempdir())
    frida_bin = tmp / f"frida-server-{frida_ver}-android-{FRIDA_ARCH}"

    if not frida_bin.exists():
        data = download(frida_url, f"frida-server {frida_ver} ({FRIDA_ARCH})")
        info("Decompressing frida-server...")
        frida_bin.write_bytes(lzma.decompress(data))

    info("Pushing frida-server to emulator...")
    run(["adb", "push", str(frida_bin), "/data/local/tmp/frida-server"])
    run(["adb", "shell", "chmod", "755", "/data/local/tmp/frida-server"])

    info("Starting frida-server...")
    run(["adb", "shell",
         "killall frida-server 2>/dev/null; "
         "nohup /data/local/tmp/frida-server -D &"])
    time.sleep(3)


# ── Step 6: Run KeyDive ──────────────────────────────────────────────

def install_keydive():
    info("Installing KeyDive...")
    run(["uv", "tool", "install", "--force", "keydive"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_keydive() -> Path:
    output_dir = Path(tempfile.gettempdir()) / "keydive_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    info("Running KeyDive to extract CDM (this may take 30-60 seconds)...")
    run(["keydive", "-o", str(output_dir), "-w", "--auto", "player"])

    # Look for .wvd first
    wvd_files = list(output_dir.rglob("*.wvd"))
    if wvd_files:
        return wvd_files[0]

    # Fall back to creating .wvd from raw credentials
    client_ids = list(output_dir.rglob("client_id.bin"))
    private_keys = list(output_dir.rglob("private_key.pem"))

    if client_ids and private_keys:
        info("Creating .wvd from extracted credentials...")
        run(["pywidevine", "create-device",
             "-t", "ANDROID", "-l", "3",
             "-k", str(private_keys[0]),
             "-c", str(client_ids[0]),
             "-o", str(output_dir)])
        wvd_files = list(output_dir.rglob("*.wvd"))
        if wvd_files:
            return wvd_files[0]

    err("CDM extraction failed.")
    err("Output directory contents:")
    for f in output_dir.rglob("*"):
        if f.is_file():
            err(f"  {f}")
    sys.exit(1)


# ── Step 7: Install .wvd ─────────────────────────────────────────────

def install_wvd(wvd_path: Path):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    dest = CONFIG_DIR / "device.wvd"
    shutil.copy2(wvd_path, dest)
    info(f"CDM installed to: {dest}")


# ── Step 8: Cleanup ──────────────────────────────────────────────────

def cleanup():
    info("Shutting down emulator...")
    run(["adb", "emu", "kill"], capture_output=True)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    print(f"Platform: {SYSTEM} ({MACHINE})")
    print(f"Emulator arch: {EMU_ARCH}")
    print()

    check_prerequisites()
    install_sdk_tools()
    install_emulator_image()
    create_avd()
    install_keydive()
    boot_emulator()
    setup_frida()
    wvd = run_keydive()
    install_wvd(wvd)
    cleanup()

    print()
    info("Done! You can now run:")
    info("  uv run python music_dl.py '<song-url>'")


if __name__ == "__main__":
    main()
