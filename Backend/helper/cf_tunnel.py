import asyncio
import os
import platform
import re
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from typing import Optional

from Backend.logger import LOGGER

_TUNNEL_PROC: Optional[asyncio.subprocess.Process] = None
_TUNNEL_URL: Optional[str] = None
_CLOUDFLARED_BIN_PATH: Optional[str] = None


def _get_cloudflared_path() -> str:
    global _CLOUDFLARED_BIN_PATH
    if _CLOUDFLARED_BIN_PATH and os.path.exists(_CLOUDFLARED_BIN_PATH):
        return _CLOUDFLARED_BIN_PATH

    bin_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "bin")
    os.makedirs(bin_dir, exist_ok=True)
    exe_name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    target = os.path.join(bin_dir, exe_name)

    if os.path.exists(target):
        _CLOUDFLARED_BIN_PATH = target
        return target

    # Check if available globally in PATH
    import shutil
    global_bin = shutil.which("cloudflared")
    if global_bin:
        _CLOUDFLARED_BIN_PATH = global_bin
        return global_bin

    _CLOUDFLARED_BIN_PATH = target
    return target


def ensure_cloudflared_binary() -> str:
    bin_path = _get_cloudflared_path()
    if os.path.exists(bin_path):
        return bin_path

    LOGGER.info("cloudflared binary not found. Downloading latest release from GitHub...")
    system = sys.platform
    machine = platform.machine().lower()

    if system == "win32":
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    elif system == "darwin":
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz" if "arm" not in machine else "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"
    else:  # Linux / Unix
        if "aarch64" in machine or "arm64" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
        elif "arm" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm"
        elif "386" in machine or "686" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-386"
        else:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

    try:
        LOGGER.info(f"Downloading cloudflared from {url}...")
        urllib.request.urlretrieve(url, bin_path)
        if system != "win32":
            st = os.stat(bin_path)
            os.chmod(bin_path, st.st_mode | stat.S_IEXEC)
        LOGGER.info(f"cloudflared binary successfully installed to {bin_path}")
        return bin_path
    except Exception as e:
        LOGGER.error(f"Failed to auto-download cloudflared: {e}")
        raise


async def start_cloudflare_tunnel(
    port: int,
    tunnel_token: str = "",
    custom_url: str = "",
    timeout: int = 40,
) -> Optional[str]:
    """
    Starts cloudflared in the background and returns the public tunnel URL.
    - If tunnel_token is given, it runs: cloudflared tunnel run --token <token>
      and returns custom_url (or Settings base_url).
    - If tunnel_token is empty, it runs quick tunnel:
      cloudflared tunnel --url http://127.0.0.1:<port>
      and extracts the https://*.trycloudflare.com link.
    """
    global _TUNNEL_PROC, _TUNNEL_URL

    if _TUNNEL_PROC is not None and _TUNNEL_URL:
        return _TUNNEL_URL

    try:
        binary = ensure_cloudflared_binary()
    except Exception as e:
        LOGGER.error(f"Cannot start Cloudflare tunnel: {e}")
        return None

    if tunnel_token.strip():
        cmd = [binary, "tunnel", "run", "--token", tunnel_token.strip()]
        LOGGER.info("Starting Cloudflare Named Tunnel with provided token...")
    else:
        cmd = [
            binary,
            "tunnel",
            "--url",
            f"http://127.0.0.1:{port}",
            "--no-autoupdate",
        ]
        LOGGER.info(f"Starting Cloudflare Quick Tunnel on port {port}...")

    try:
        _TUNNEL_PROC = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        LOGGER.error(f"Failed to spawn cloudflared process: {e}")
        return None

    if tunnel_token.strip():
        # For named tunnels, URL is your pre-configured custom domain
        if custom_url.strip():
            _TUNNEL_URL = custom_url.strip().rstrip("/")
            return _TUNNEL_URL
        return None

    # For Quick Tunnel, extract the trycloudflare.com URL from stderr/stdout
    url_pattern = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        if _TUNNEL_PROC.returncode is not None:
            LOGGER.error(f"cloudflared exited unexpectedly with code {_TUNNEL_PROC.returncode}")
            return None

        try:
            line = await asyncio.wait_for(_TUNNEL_PROC.stderr.readline(), timeout=2.0)
            if not line:
                await asyncio.sleep(0.5)
                continue
            text = line.decode("utf-8", errors="ignore").strip()
            match = url_pattern.search(text)
            if match:
                _TUNNEL_URL = match.group(0).rstrip("/")
                LOGGER.info(f"Cloudflare Quick Tunnel established: {_TUNNEL_URL}")
                return _TUNNEL_URL
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            LOGGER.warning(f"Error reading cloudflared output: {e}")
            break

    LOGGER.error("Timed out waiting for Cloudflare Tunnel URL")
    return None


async def stop_cloudflare_tunnel():
    global _TUNNEL_PROC, _TUNNEL_URL
    if _TUNNEL_PROC is not None:
        LOGGER.info("Stopping Cloudflare tunnel process...")
        try:
            _TUNNEL_PROC.terminate()
            await asyncio.wait_for(_TUNNEL_PROC.wait(), timeout=5.0)
        except Exception:
            try:
                _TUNNEL_PROC.kill()
            except Exception:
                pass
        _TUNNEL_PROC = None
        _TUNNEL_URL = None
