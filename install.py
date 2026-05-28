#!/usr/bin/env python3
"""
PdfToAudiobook — cross-platform setup script.

Usage:
    python install.py

What it does:
    1. Checks your Python version
    2. Detects your OS, CPU architecture, and GPU
    3. Installs torch + torchaudio from the correct PyTorch variant
       (CPU / CUDA 11.8 / CUDA 12.1 / CUDA 12.4 / Apple MPS)
    4. Installs all remaining packages from requirements.txt
    5. Checks that ffmpeg is available (required for MP3 joining)

Supported platforms
    OS          : Windows, macOS, Linux
    Arch        : x86_64, arm64 (Apple Silicon + Linux aarch64)
    Python      : 3.9 – 3.13
    GPU         : NVIDIA CUDA, Apple MPS, or CPU-only

NOT supported
    macOS Intel (x86_64): torchcodec (audio backend) has no macOS Intel wheels.
    Use macOS arm64 or Linux instead.
"""

import re
import shutil
import subprocess
import sys
import platform
from pathlib import Path

HERE = Path(__file__).parent
PYTHON_MIN = (3, 9)
PYTHON_MAX = (3, 13)

PYTORCH_URLS = {
    "cpu":   "https://download.pytorch.org/whl/cpu",
    "cu118": "https://download.pytorch.org/whl/cu118",
    "cu121": "https://download.pytorch.org/whl/cu121",
    "cu124": "https://download.pytorch.org/whl/cu124",
}


# ─────────────────────────────── helpers ──────────────────────────────────────

def _pip(*args):
    cmd = [sys.executable, "-m", "pip", "install", *args]
    print(f"\n  $ {' '.join(str(a) for a in cmd)}\n")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        _fail(f"pip exited with code {result.returncode}")


def _ok(msg):
    print(f"  \033[32m✓\033[0m  {msg}" if sys.stdout.isatty() else f"  ✓  {msg}")


def _warn(msg):
    print(f"  \033[33m⚠\033[0m  {msg}" if sys.stdout.isatty() else f"  ⚠  {msg}")


def _fail(msg):
    print(f"  \033[31m✗\033[0m  {msg}" if sys.stdout.isatty() else f"  ✗  {msg}")
    sys.exit(1)


def _section(title):
    print(f"\n  [ {title} ]")


def _banner():
    w = 52
    print()
    print("  ╔" + "═" * (w - 2) + "╗")
    print("  ║" + "  PdfToAudiobook — Setup".center(w - 2) + "║")
    print("  ╚" + "═" * (w - 2) + "╝")
    print()


# ─────────────────────────────── checks ───────────────────────────────────────

def check_python():
    v = sys.version_info[:2]
    label = f"Python {v[0]}.{v[1]}.{sys.version_info.micro}"
    if v < PYTHON_MIN:
        _fail(f"{label} is too old — minimum is {PYTHON_MIN[0]}.{PYTHON_MIN[1]}")
    if v > PYTHON_MAX:
        _warn(f"{label} is above the tested maximum ({PYTHON_MAX[0]}.{PYTHON_MAX[1]}) — may still work")
    else:
        _ok(label)


def check_macos_intel():
    if platform.system() == "Darwin" and platform.machine() == "x86_64":
        _fail(
            "macOS Intel (x86_64) is not supported.\n"
            "  torchcodec — required by coqui-tts — has no macOS Intel wheels.\n"
            "  Use macOS arm64 (Apple Silicon), Linux, or Windows instead."
        )


# ─────────────────────────────── GPU detection ────────────────────────────────

def _nvcc_cuda_tag():
    """Try to read CUDA version from nvcc. Returns e.g. 'cu121' or None."""
    nvcc = shutil.which("nvcc")
    if not nvcc:
        return None
    r = subprocess.run([nvcc, "--version"], capture_output=True, text=True)
    m = re.search(r"release (\d+)\.(\d+)", r.stdout)
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2))
    # Map to nearest supported tag
    if major == 11:
        return "cu118"
    if major == 12:
        if minor <= 1:
            return "cu121"
        return "cu124"
    return "cu124"  # newer than known — try latest


def _ask_cuda_tag(gpu_name):
    """Prompt user to select CUDA version when auto-detection fails."""
    print(f"\n  GPU found: {gpu_name}")
    print("  Could not auto-detect CUDA version.")
    print("  Check 'nvidia-smi' output (top-right: 'CUDA Version') and choose:\n")
    opts = [
        ("1", "cu118", "CUDA 11.8"),
        ("2", "cu121", "CUDA 12.1  ← most common"),
        ("3", "cu124", "CUDA 12.4"),
        ("4", "cpu",   "No CUDA / CPU only"),
    ]
    for num, tag, label in opts:
        print(f"    [{num}]  {label}")
    while True:
        raw = input("\n  Choice [1-4]: ").strip()
        for num, tag, _ in opts:
            if raw == num:
                return tag
        print("  Invalid — enter 1, 2, 3, or 4.")


def detect_pytorch_variant():
    """
    Returns (variant_tag, index_url) where index_url may be None
    (meaning: install from PyPI, which is correct for Apple Silicon).
    """
    system = platform.system()
    machine = platform.machine()

    # ── Apple Silicon ──────────────────────────────────────────────────────
    if system == "Darwin" and machine == "arm64":
        _ok("Apple Silicon (arm64) — PyPI torch includes MPS acceleration")
        return "mps", None

    # ── NVIDIA CUDA ────────────────────────────────────────────────────────
    if shutil.which("nvidia-smi"):
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            gpu_name = r.stdout.strip().splitlines()[0]
            tag = _nvcc_cuda_tag()
            if tag:
                _ok(f"{gpu_name} — CUDA {tag}")
            else:
                tag = _ask_cuda_tag(gpu_name)
            if tag == "cpu":
                _warn("Falling back to CPU-only install")
                return "cpu", PYTORCH_URLS["cpu"]
            return tag, PYTORCH_URLS[tag]

    # ── CPU fallback ───────────────────────────────────────────────────────
    _ok("No GPU detected — CPU-only install")
    return "cpu", PYTORCH_URLS["cpu"]


# ─────────────────────────────── ffmpeg ───────────────────────────────────────

def check_ffmpeg():
    if shutil.which("ffmpeg"):
        _ok("ffmpeg found in PATH")
        return

    system = platform.system()
    installs = {
        "Windows": (
            "  winget install ffmpeg\n"
            "  or download from https://ffmpeg.org/download.html\n"
            "  and add the bin/ folder to your PATH"
        ),
        "Darwin": "  brew install ffmpeg",
        "Linux":  (
            "  sudo apt install ffmpeg          # Debian / Ubuntu\n"
            "  sudo dnf install ffmpeg          # Fedora / RHEL\n"
            "  sudo pacman -S ffmpeg            # Arch"
        ),
    }.get(system, "  See https://ffmpeg.org/download.html")

    _warn("ffmpeg not found — install it to use the MP3 join feature:")
    print()
    for line in installs.splitlines():
        print(f"    {line.strip()}")


# ─────────────────────────────── main ─────────────────────────────────────────

def main():
    _banner()

    _section("Python")
    check_python()

    _section("Platform")
    check_macos_intel()

    _section("GPU / PyTorch variant")
    variant, index_url = detect_pytorch_variant()

    _section(f"Installing torch + torchaudio  [{variant}]")
    if index_url:
        _pip("torch", "torchaudio", "--index-url", index_url)
    else:
        # Apple Silicon: PyPI torch is correct and includes MPS
        _pip("torch", "torchaudio")

    _section("Installing remaining packages")
    _pip("-r", str(HERE / "requirements.txt"))

    _section("ffmpeg")
    check_ffmpeg()

    print()
    print("  ─" * 26)
    _ok("Setup complete.  Run with:  python main.py")
    print()


if __name__ == "__main__":
    main()
