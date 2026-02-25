"""Shared helper for sending HTTP requests to the in-container daemon via docker exec curl."""

import json
import struct

from infra.core.sandbox import get_container

DAEMON_PORT = 8331


def request_sync(
    sandbox_id: str,
    method: str,
    path: str,
    body: dict | None = None,
    timeout: int = 0,
) -> dict:
    """Send an HTTP request to the daemon inside the container via docker exec.

    For non-GET requests with a body, pipes the JSON payload through stdin
    to avoid 'argument list too long' errors with large payloads.

    Args:
        sandbox_id: The container ID.
        method: HTTP method (GET, POST, PUT, etc.).
        path: URL path (e.g. "/exec", "/files/workspace/test.txt").
        body: JSON body for non-GET requests.
        timeout: curl --max-time in seconds. 0 means no limit.

    Returns:
        Parsed JSON response from the daemon.
    """
    container = get_container(sandbox_id)
    url = f"http://localhost:{DAEMON_PORT}{path}"

    timeout_args = ["--max-time", str(timeout)] if timeout > 0 else []

    if method == "GET":
        cmd = ["curl", "-sf"] + timeout_args + [url]
        exit_code, output = container.exec_run(cmd, demux=True)
        stdout = output[0].decode("utf-8", errors="replace") if output and output[0] else ""
        stderr = output[1].decode("utf-8", errors="replace") if output and output[1] else ""

        if exit_code != 0:
            raise RuntimeError(f"Daemon request failed (exit {exit_code}): {stderr or stdout}")
        return json.loads(stdout)

    # Non-GET: pipe body through stdin to avoid argument length limits
    body_bytes = json.dumps(body or {}).encode("utf-8")
    cmd = (
        ["curl", "-sf"]
        + timeout_args
        + ["-X", method, "-H", "Content-Type: application/json", "-d", "@-", url]
    )

    api = container.client.api
    exec_id = api.exec_create(container.id, cmd, stdin=True, stdout=True, stderr=True)["Id"]
    sock = api.exec_start(exec_id, socket=True)

    # Write body to stdin, then close write end
    sock._sock.sendall(body_bytes)
    sock._sock.shutdown(1)  # SHUT_WR

    # Read all output
    raw = b""
    while True:
        chunk = sock.read(65536)
        if not chunk:
            break
        raw += chunk
    sock.close()

    stdout, stderr = _demux_docker_stream(raw)

    inspect = api.exec_inspect(exec_id)
    exit_code = inspect.get("ExitCode", -1)

    if exit_code != 0:
        raise RuntimeError(f"Daemon request failed (exit {exit_code}): {stderr or stdout}")

    return json.loads(stdout)


def _demux_docker_stream(data: bytes) -> tuple[str, str]:
    """Demux a Docker multiplexed stream into stdout and stderr strings.

    Docker stream format: 8-byte header per frame.
    Header: [stream_type(1), 0, 0, 0, size(4 bytes, big-endian)]
    stream_type: 1=stdout, 2=stderr
    """
    stdout_parts = []
    stderr_parts = []
    offset = 0

    while offset + 8 <= len(data):
        stream_type = data[offset]
        frame_size = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        offset += 8
        if offset + frame_size > len(data):
            break
        payload = data[offset : offset + frame_size].decode("utf-8", errors="replace")
        if stream_type == 1:
            stdout_parts.append(payload)
        elif stream_type == 2:
            stderr_parts.append(payload)
        offset += frame_size

    return "".join(stdout_parts), "".join(stderr_parts)
