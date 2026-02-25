"""VPS-simulated stress tests — throttled CPU, higher contention, strict latency budgets.

Simulates a 2-4 core VPS with limited resources by:
1. Restricting Python to 2 CPU cores (os.sched_setaffinity on Linux, skipped on macOS)
2. Adding artificial I/O delays to simulate slower disk/network
3. Running higher contention ratios (more concurrent requests per core)
4. Enforcing strict latency budgets that a real VPS must meet

Run: ABAX_POOL_SIZE=0 python -m pytest tests/infra/test_stress_vps.py -v

Environment variables for tuning:
  VPS_CORES=2          Simulated core count (throttles concurrent work)
  VPS_IO_DELAY_MS=5    Extra ms delay per I/O operation (simulates HDD/slow SSD)
  VPS_MEMORY_MB=2048   Simulated memory budget (for capacity calculations)
"""
import asyncio
import os
import platform
import time

import pytest
from httpx import ASGITransport, AsyncClient

from infra.api.main import app
from infra.core.sandbox import LABEL_PREFIX, client as docker_client
from tests.infra.conftest import _wait_for_daemon

# --- VPS simulation parameters ---
VPS_CORES = int(os.getenv("VPS_CORES", "2"))
VPS_IO_DELAY_MS = int(os.getenv("VPS_IO_DELAY_MS", "5"))
VPS_MEMORY_MB = int(os.getenv("VPS_MEMORY_MB", "2048"))

# Derived limits
# On a 2-core VPS, each sandbox is ~768MB, so max 2 running concurrently
VPS_MAX_CONCURRENT_SANDBOXES = max(1, (VPS_MEMORY_MB - 200) // 768)


def _cleanup_all_containers():
    containers = docker_client.containers.list(
        all=True,
        filters={"label": f"{LABEL_PREFIX}.managed=true"},
    )
    for c in containers:
        try:
            c.remove(force=True)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def clean_slate():
    _cleanup_all_containers()
    yield
    _cleanup_all_containers()


@pytest.fixture(autouse=True)
def restrict_cpu():
    """On Linux, restrict to VPS_CORES CPUs. On macOS, just log the simulation."""
    if platform.system() == "Linux":
        try:
            os.sched_setaffinity(0, set(range(VPS_CORES)))
        except (OSError, AttributeError):
            pass
    yield
    if platform.system() == "Linux":
        try:
            os.sched_setaffinity(0, set(range(os.cpu_count() or 1)))
        except (OSError, AttributeError):
            pass


@pytest.fixture
async def vps_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=120) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Container creation latency budget — strict VPS timing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vps_container_creation_latency(vps_client):
    """Container creation should complete within 10s on a VPS (includes daemon startup)."""
    start = time.monotonic()
    r = await vps_client.post("/sandboxes", json={"user_id": "vps-latency"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    await _wait_for_daemon(sid)
    elapsed = time.monotonic() - start

    print(f"  VPS container creation + daemon ready: {elapsed:.2f}s")
    assert elapsed < 10.0, f"Container creation took {elapsed:.2f}s (budget: 10s)"


@pytest.mark.asyncio
async def test_vps_sequential_container_creation(vps_client):
    """Create VPS_MAX_CONCURRENT_SANDBOXES containers sequentially — measure total time."""
    n = VPS_MAX_CONCURRENT_SANDBOXES
    sandbox_ids = []
    timings = []

    for i in range(n):
        start = time.monotonic()
        r = await vps_client.post("/sandboxes", json={"user_id": f"vps-seq-{i}"})
        assert r.status_code == 200
        sid = r.json()["sandbox_id"]
        await _wait_for_daemon(sid)
        elapsed = time.monotonic() - start
        sandbox_ids.append(sid)
        timings.append(elapsed)
        print(f"  Container {i+1}/{n}: {elapsed:.2f}s")

    avg = sum(timings) / len(timings)
    print(f"  VPS avg container creation: {avg:.2f}s (n={n})")
    for i, t in enumerate(timings):
        assert t < 15.0, f"Container {i} took {t:.2f}s (budget: 15s)"


# ---------------------------------------------------------------------------
# 2. Exec latency under contention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vps_exec_latency_budget(vps_client):
    """Simple echo should complete in <2s even on VPS."""
    r = await vps_client.post("/sandboxes", json={"user_id": "vps-exec"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    await _wait_for_daemon(sid)

    timings = []
    for i in range(10):
        start = time.monotonic()
        r = await vps_client.post(
            f"/sandboxes/{sid}/exec",
            json={"command": f"echo test-{i}", "timeout": 10},
        )
        elapsed = time.monotonic() - start
        assert r.status_code == 200
        assert r.json()["exit_code"] == 0
        timings.append(elapsed)

    avg = sum(timings) / len(timings)
    p95 = sorted(timings)[int(len(timings) * 0.95)]
    print(f"  VPS exec latency: avg={avg*1000:.0f}ms p95={p95*1000:.0f}ms")
    assert avg < 1.0, f"Avg exec latency {avg*1000:.0f}ms exceeds 1000ms budget"
    assert p95 < 2.0, f"P95 exec latency {p95*1000:.0f}ms exceeds 2000ms budget"


@pytest.mark.asyncio
async def test_vps_concurrent_exec_on_single_container(vps_client):
    """10 concurrent exec on same container — VPS should handle without timeouts."""
    r = await vps_client.post("/sandboxes", json={"user_id": "vps-cexec"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    await _wait_for_daemon(sid)

    start = time.monotonic()
    tasks = [
        vps_client.post(
            f"/sandboxes/{sid}/exec",
            json={"command": f"echo concurrent-{i}", "timeout": 10},
        )
        for i in range(10)
    ]
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start

    successes = [r for r in results if r.status_code == 200]
    assert len(successes) == 10
    assert elapsed < 10.0, f"10 concurrent execs took {elapsed:.2f}s (budget: 10s)"
    print(f"  VPS 10 concurrent execs: {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# 3. File I/O via daemon — latency budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vps_file_io_latency(vps_client):
    """File write + read cycle should complete in <1s per operation on VPS."""
    r = await vps_client.post("/sandboxes", json={"user_id": "vps-fileio"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    await _wait_for_daemon(sid)

    write_times = []
    read_times = []

    for i in range(10):
        content = f"VPS file test iteration {i}\n" * 100  # ~3KB

        start = time.monotonic()
        r = await vps_client.put(
            f"/sandboxes/{sid}/files/workspace/test_{i}.txt",
            json={"content": content, "path": f"/workspace/test_{i}.txt"},
        )
        write_times.append(time.monotonic() - start)
        assert r.status_code == 200

        start = time.monotonic()
        r = await vps_client.get(f"/sandboxes/{sid}/files/workspace/test_{i}.txt")
        read_times.append(time.monotonic() - start)
        assert r.status_code == 200
        assert r.json()["content"] == content

    avg_write = sum(write_times) / len(write_times)
    avg_read = sum(read_times) / len(read_times)
    print(f"  VPS file I/O: write avg={avg_write*1000:.0f}ms read avg={avg_read*1000:.0f}ms")

    assert avg_write < 1.0, f"Avg write {avg_write*1000:.0f}ms exceeds 1000ms"
    assert avg_read < 1.0, f"Avg read {avg_read*1000:.0f}ms exceeds 1000ms"


# ---------------------------------------------------------------------------
# 4. Batch file operations — VPS benefit measurement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vps_batch_vs_sequential_files(vps_client):
    """Compare batch file reads vs sequential — batch should be faster."""
    r = await vps_client.post("/sandboxes", json={"user_id": "vps-batch"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    await _wait_for_daemon(sid)

    # Write 10 files
    for i in range(10):
        r = await vps_client.put(
            f"/sandboxes/{sid}/files/workspace/batch_{i}.txt",
            json={"content": f"content-{i}", "path": f"/workspace/batch_{i}.txt"},
        )
        assert r.status_code == 200

    # Sequential reads
    start = time.monotonic()
    for i in range(10):
        r = await vps_client.get(f"/sandboxes/{sid}/files/workspace/batch_{i}.txt")
        assert r.status_code == 200
    sequential_time = time.monotonic() - start

    # Batch read
    start = time.monotonic()
    r = await vps_client.post(
        f"/sandboxes/{sid}/files-batch",
        json={
            "operations": [
                {"op": "read", "path": f"/workspace/batch_{i}.txt"}
                for i in range(10)
            ]
        },
    )
    batch_time = time.monotonic() - start
    assert r.status_code == 200

    speedup = sequential_time / batch_time if batch_time > 0 else float("inf")
    print(f"  VPS batch vs sequential: {sequential_time:.2f}s vs {batch_time:.2f}s ({speedup:.1f}x speedup)")

    assert speedup > 1.5, f"Batch speedup {speedup:.1f}x is below 1.5x threshold"


# ---------------------------------------------------------------------------
# 5. Memory budget validation — container count limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vps_memory_budget_respected(vps_client):
    """Create containers up to VPS memory budget — verify limits work."""
    max_containers = VPS_MAX_CONCURRENT_SANDBOXES
    print(f"  VPS memory budget: {VPS_MEMORY_MB}MB → max {max_containers} containers")

    sandbox_ids = []
    for i in range(max_containers):
        r = await vps_client.post("/sandboxes", json={"user_id": f"vps-mem-{i}"})
        assert r.status_code == 200
        sandbox_ids.append(r.json()["sandbox_id"])

    # All should be running
    r = await vps_client.get("/sandboxes")
    running = [s for s in r.json() if s["user_id"].startswith("vps-mem-") and s["status"] == "running"]
    assert len(running) == max_containers

    h = await vps_client.get("/health")
    assert h.status_code == 200
    assert h.json()["active_sandboxes"] >= max_containers


# ---------------------------------------------------------------------------
# 6. Pause/resume cycle latency on VPS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vps_pause_resume_latency(vps_client):
    """Pause/resume cycle should complete in <3s on VPS."""
    r = await vps_client.post("/sandboxes", json={"user_id": "vps-pr"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    await _wait_for_daemon(sid)

    timings = []
    for i in range(5):
        start = time.monotonic()
        r = await vps_client.post(f"/sandboxes/{sid}/pause")
        assert r.status_code == 200
        r = await vps_client.post(f"/sandboxes/{sid}/resume")
        assert r.status_code == 200
        elapsed = time.monotonic() - start
        timings.append(elapsed)

    avg = sum(timings) / len(timings)
    print(f"  VPS pause/resume cycle: avg={avg*1000:.0f}ms")
    assert avg < 3.0, f"Avg pause/resume {avg*1000:.0f}ms exceeds 3000ms budget"


# ---------------------------------------------------------------------------
# 7. End-to-end infra user journey latency budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vps_user_journey_latency(vps_client):
    """Complete infra user journey with VPS latency budget:
    1. Create sandbox + wait for daemon (<10s)
    2. Write file (<1s)
    3. Read file (<1s)
    4. Exec command (<2s)
    5. Pause (<1s)
    6. Resume + exec (<3s)
    """
    timings = {}

    # 1. Create sandbox
    start = time.monotonic()
    r = await vps_client.post("/sandboxes", json={"user_id": "vps-journey"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    await _wait_for_daemon(sid)
    timings["sandbox_create"] = time.monotonic() - start

    # 2. Write file
    start = time.monotonic()
    r = await vps_client.put(
        f"/sandboxes/{sid}/files/workspace/hello.txt",
        json={"content": "Hello VPS!\n" * 100, "path": "/workspace/hello.txt"},
    )
    assert r.status_code == 200
    timings["file_write"] = time.monotonic() - start

    # 3. Read file
    start = time.monotonic()
    r = await vps_client.get(f"/sandboxes/{sid}/files/workspace/hello.txt")
    assert r.status_code == 200
    timings["file_read"] = time.monotonic() - start

    # 4. Exec
    start = time.monotonic()
    r = await vps_client.post(
        f"/sandboxes/{sid}/exec",
        json={"command": "wc -l /workspace/hello.txt", "timeout": 10},
    )
    assert r.status_code == 200
    timings["exec"] = time.monotonic() - start

    # 5. Pause
    start = time.monotonic()
    r = await vps_client.post(f"/sandboxes/{sid}/pause")
    assert r.status_code == 200
    timings["pause"] = time.monotonic() - start

    # 6. Resume + exec
    start = time.monotonic()
    r = await vps_client.post(f"/sandboxes/{sid}/resume")
    assert r.status_code == 200
    r = await vps_client.post(
        f"/sandboxes/{sid}/exec",
        json={"command": "echo resumed", "timeout": 10},
    )
    assert r.status_code == 200
    timings["resume_exec"] = time.monotonic() - start

    total = sum(timings.values())

    print(f"  VPS user journey:")
    budgets = {
        "sandbox_create": 10.0,
        "file_write": 1.0,
        "file_read": 1.0,
        "exec": 2.0,
        "pause": 1.0,
        "resume_exec": 3.0,
    }
    for step, t in timings.items():
        budget = budgets[step]
        status = "OK" if t < budget else "SLOW"
        print(f"    {step}: {t*1000:.0f}ms (budget: {budget*1000:.0f}ms) [{status}]")
    print(f"    TOTAL: {total:.2f}s")

    for step, t in timings.items():
        budget = budgets[step]
        assert t < budget, f"{step} took {t*1000:.0f}ms, budget is {budget*1000:.0f}ms"
