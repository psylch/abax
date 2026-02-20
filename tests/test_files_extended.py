"""Tests for directory listing and binary file write functions."""
import pytest

from gateway.files import list_dir, write_file_bytes, read_file_bytes


@pytest.mark.asyncio
async def test_list_dir(client, sandbox_id):
    """list_dir returns entries for a known directory."""
    entries = await list_dir(sandbox_id, "/tmp")
    assert isinstance(entries, list)
    # /tmp exists and is listable (may be empty, but should not error)


@pytest.mark.asyncio
async def test_list_dir_with_files(client, sandbox_id):
    """list_dir shows files that were written to the container."""
    # Write a file so /data has known content
    await client.put(
        f"/sandboxes/{sandbox_id}/files/data/listing_test.txt",
        json={"content": "hello", "path": "/data/listing_test.txt"},
    )
    entries = await list_dir(sandbox_id, "/data")
    names = [e["name"] for e in entries]
    assert "listing_test.txt" in names
    # Verify entry structure
    entry = next(e for e in entries if e["name"] == "listing_test.txt")
    assert entry["is_dir"] is False
    assert entry["size"] >= 0


@pytest.mark.asyncio
async def test_list_dir_not_found(client, sandbox_id):
    """list_dir raises FileNotFoundError for non-existent directory."""
    with pytest.raises(FileNotFoundError):
        await list_dir(sandbox_id, "/nonexistent_dir_xyz")


@pytest.mark.asyncio
async def test_write_and_read_file_bytes(client, sandbox_id):
    """write_file_bytes writes binary data that read_file_bytes can read back."""
    binary_data = bytes(range(256))  # all byte values 0x00-0xFF
    path = "/data/binary_test.bin"

    await write_file_bytes(sandbox_id, path, binary_data)
    read_back, name = await read_file_bytes(sandbox_id, path)

    assert read_back == binary_data
    assert name == "binary_test.bin"
