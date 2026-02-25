import asyncio
import hashlib
import hmac
import io
import os
import tarfile
import time

from infra.core.daemon import request_sync
from infra.core.sandbox import get_container

SIGN_SECRET = os.getenv("ABAX_SIGN_SECRET", "dev-secret-change-in-prod")
SIGN_EXPIRY = 3600


async def read_file(sandbox_id: str, path: str) -> str:
    """Read a text file via the daemon."""
    url_path = path.lstrip("/")
    data = await asyncio.to_thread(
        request_sync, sandbox_id, "GET", f"/files/{url_path}"
    )
    if "error" in data:
        raise FileNotFoundError(data["error"])
    return data["content"]


async def write_file(sandbox_id: str, path: str, content: str) -> None:
    """Write a text file via the daemon."""
    url_path = path.lstrip("/")
    data = await asyncio.to_thread(
        request_sync, sandbox_id, "PUT", f"/files/{url_path}", {"content": content}
    )
    if "error" in data:
        raise PermissionError(data["error"])


async def list_dir(sandbox_id: str, path: str) -> list[dict]:
    """List directory contents via the daemon."""
    url_path = path.lstrip("/")
    data = await asyncio.to_thread(
        request_sync, sandbox_id, "GET", f"/ls/{url_path}"
    )
    if "error" in data:
        raise FileNotFoundError(data["error"])
    return data["entries"]


async def batch_file_ops(sandbox_id: str, operations: list[dict]) -> list[dict]:
    """Perform batch file operations via the daemon."""
    data = await asyncio.to_thread(
        request_sync, sandbox_id, "POST", "/files/batch", {"operations": operations}
    )
    return data["results"]


# --- Binary file operations (keep using Docker API for efficiency) ---


def _read_file_bytes_sync(sandbox_id: str, path: str) -> tuple[bytes, str]:
    container = get_container(sandbox_id)
    bits, _ = container.get_archive(path)
    tarstream = io.BytesIO()
    for chunk in bits:
        tarstream.write(chunk)
    tarstream.seek(0)
    with tarfile.open(fileobj=tarstream) as tar:
        member = tar.getmembers()[0]
        f = tar.extractfile(member)
        return f.read(), member.name


async def read_file_bytes(sandbox_id: str, path: str) -> tuple[bytes, str]:
    return await asyncio.to_thread(_read_file_bytes_sync, sandbox_id, path)


def _write_file_bytes_sync(sandbox_id: str, path: str, data: bytes) -> None:
    container = get_container(sandbox_id)
    tarstream = io.BytesIO()
    tarinfo = tarfile.TarInfo(name=os.path.basename(path))
    tarinfo.size = len(data)
    with tarfile.open(fileobj=tarstream, mode="w") as tar:
        tar.addfile(tarinfo, io.BytesIO(data))
    tarstream.seek(0)
    container.put_archive(os.path.dirname(path) or "/", tarstream)


async def write_file_bytes(sandbox_id: str, path: str, data: bytes) -> None:
    await asyncio.to_thread(_write_file_bytes_sync, sandbox_id, path, data)


# --- Download token (unchanged) ---


def generate_download_token(sandbox_id: str, path: str) -> str:
    expires = int(time.time()) + SIGN_EXPIRY
    payload = f"{sandbox_id}:{path}:{expires}"
    sig = hmac.new(SIGN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{sandbox_id}:{path}:{expires}:{sig}"


def verify_download_token(token: str) -> tuple[str, str]:
    parts = token.split(":", 3)
    if len(parts) != 4:
        raise ValueError("invalid token format")

    sandbox_id, path, expires_str, sig = parts
    expires = int(expires_str)

    if time.time() > expires:
        raise ValueError("token expired")

    expected_payload = f"{sandbox_id}:{path}:{expires}"
    expected_sig = hmac.new(
        SIGN_SECRET.encode(), expected_payload.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("invalid signature")

    return sandbox_id, path
