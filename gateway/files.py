import hashlib
import hmac
import io
import os
import tarfile
import time

from gateway.sandbox import get_container

SIGN_SECRET = os.getenv("ABAX_SIGN_SECRET", "dev-secret-change-in-prod")
SIGN_EXPIRY = 3600  # 1 hour


def read_file(sandbox_id: str, path: str) -> str:
    container = get_container(sandbox_id)
    exit_code, output = container.exec_run(["cat", path])
    if exit_code != 0:
        raise FileNotFoundError(f"{path}: {output.decode('utf-8', errors='replace')}")
    return output.decode("utf-8", errors="replace")


def write_file(sandbox_id: str, path: str, content: str) -> None:
    container = get_container(sandbox_id)

    # Use put_archive to write file into container
    data = content.encode("utf-8")
    tarstream = io.BytesIO()
    tarinfo = tarfile.TarInfo(name=os.path.basename(path))
    tarinfo.size = len(data)
    with tarfile.open(fileobj=tarstream, mode="w") as tar:
        tar.addfile(tarinfo, io.BytesIO(data))
    tarstream.seek(0)

    container.put_archive(os.path.dirname(path) or "/", tarstream)


def read_file_bytes(sandbox_id: str, path: str) -> tuple[bytes, str]:
    """Read file as bytes, return (data, filename)."""
    container = get_container(sandbox_id)
    bits, _ = container.get_archive(path)
    # get_archive returns a tar stream
    tarstream = io.BytesIO()
    for chunk in bits:
        tarstream.write(chunk)
    tarstream.seek(0)
    with tarfile.open(fileobj=tarstream) as tar:
        member = tar.getmembers()[0]
        f = tar.extractfile(member)
        return f.read(), member.name


def generate_download_token(sandbox_id: str, path: str) -> str:
    expires = int(time.time()) + SIGN_EXPIRY
    payload = f"{sandbox_id}:{path}:{expires}"
    sig = hmac.new(SIGN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{sandbox_id}:{path}:{expires}:{sig}"


def verify_download_token(token: str) -> tuple[str, str]:
    """Returns (sandbox_id, path) or raises ValueError."""
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
