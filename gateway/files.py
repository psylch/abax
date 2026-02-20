import asyncio
import hashlib
import hmac
import io
import json
import os
import tarfile
import time

from gateway.sandbox import get_container

SIGN_SECRET = os.getenv("ABAX_SIGN_SECRET", "dev-secret-change-in-prod")
SIGN_EXPIRY = 3600


def _read_file_sync(sandbox_id: str, path: str) -> str:
    container = get_container(sandbox_id)
    exit_code, output = container.exec_run(["cat", path])
    if exit_code != 0:
        raise FileNotFoundError(f"{path}: {output.decode('utf-8', errors='replace')}")
    return output.decode("utf-8", errors="replace")


async def read_file(sandbox_id: str, path: str) -> str:
    return await asyncio.to_thread(_read_file_sync, sandbox_id, path)


def _write_file_sync(sandbox_id: str, path: str, content: str) -> None:
    container = get_container(sandbox_id)
    data = content.encode("utf-8")
    tarstream = io.BytesIO()
    tarinfo = tarfile.TarInfo(name=os.path.basename(path))
    tarinfo.size = len(data)
    with tarfile.open(fileobj=tarstream, mode="w") as tar:
        tar.addfile(tarinfo, io.BytesIO(data))
    tarstream.seek(0)
    container.put_archive(os.path.dirname(path) or "/", tarstream)


async def write_file(sandbox_id: str, path: str, content: str) -> None:
    await asyncio.to_thread(_write_file_sync, sandbox_id, path, content)


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


_LIST_DIR_SCRIPT = """
import os, json, sys
p = sys.argv[1]
entries = [
    {"name": e, "is_dir": os.path.isdir(os.path.join(p, e)),
     "size": os.path.getsize(os.path.join(p, e)) if not os.path.isdir(os.path.join(p, e)) else -1}
    for e in sorted(os.listdir(p))
]
print(json.dumps(entries))
""".strip()


def _list_dir_sync(sandbox_id: str, path: str) -> list[dict]:
    """List directory contents inside the container."""
    container = get_container(sandbox_id)
    exit_code, output = container.exec_run(
        ["python3", "-c", _LIST_DIR_SCRIPT, path]
    )
    if exit_code != 0:
        raise FileNotFoundError(f"{path}: {output.decode('utf-8', errors='replace')}")
    return json.loads(output.decode("utf-8"))


async def list_dir(sandbox_id: str, path: str) -> list[dict]:
    return await asyncio.to_thread(_list_dir_sync, sandbox_id, path)


def _write_file_bytes_sync(sandbox_id: str, path: str, data: bytes) -> None:
    """Write binary data to a file inside the container."""
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
