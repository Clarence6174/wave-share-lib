"""
Unit and integration tests for WaveShare.

Run with:
    pytest tests/ -v --cov=waveshare
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from waveshare.exceptions import IntegrityError, PeerNotFoundError, TransferAbortedError
from waveshare.manager import WaveShareManager, _MAGIC, _encode_header
from waveshare.models import Peer, ShareIntent, TransferResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_file(tmp_path: Path) -> Path:
    """Create a 1 MiB temporary file filled with pseudo-random bytes."""
    p = tmp_path / "test_payload.bin"
    p.write_bytes(os.urandom(1024 * 1024))
    return p


@pytest.fixture
def sample_peer() -> Peer:
    return Peer(
        device_id="AA:BB:CC:DD:EE:FF",
        name="Bob",
        address="127.0.0.1",
        port=0,  # will be filled by integration test
        rssi=-55,
    )


# ── ShareIntent tests ─────────────────────────────────────────────────────────

class TestShareIntent:
    def test_basic_fields(self, tmp_file: Path) -> None:
        intent = ShareIntent(tmp_file)
        assert intent.filename == "test_payload.bin"
        assert intent.size == 1024 * 1024
        assert intent.checksum_algorithm == "sha256"

    def test_mimetype_pdf(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4")
        intent = ShareIntent(f)
        assert intent.mimetype == "application/pdf"

    def test_mimetype_unknown(self, tmp_path: Path) -> None:
        f = tmp_path / "blob.xyz123"
        f.write_bytes(b"\x00" * 64)
        intent = ShareIntent(f)
        assert intent.mimetype == "application/octet-stream"

    def test_to_dict_keys(self, tmp_file: Path) -> None:
        d = ShareIntent(tmp_file).to_dict()
        assert set(d.keys()) == {"filename", "size", "mimetype", "checksum_algorithm"}

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ShareIntent(tmp_path / "ghost.txt")


# ── TransferResult tests ──────────────────────────────────────────────────────

class TestTransferResult:
    def test_throughput(self) -> None:
        r = TransferResult(
            success=True,
            filename="f.bin",
            bytes_transferred=10 * 1024 * 1024,  # 10 MiB
            duration_seconds=2.0,
        )
        assert abs(r.throughput_mbps - 5.0) < 0.01  # type: ignore[operator]

    def test_throughput_none_when_no_duration(self) -> None:
        r = TransferResult(success=True, filename="f", bytes_transferred=100)
        assert r.throughput_mbps is None

    def test_str_success(self) -> None:
        r = TransferResult(success=True, filename="hello.txt", bytes_transferred=42)
        assert "hello.txt" in str(r)
        assert "✓" in str(r)

    def test_str_failure(self) -> None:
        r = TransferResult(
            success=False, filename="hello.txt", bytes_transferred=0, error="timeout"
        )
        assert "✗" in str(r)
        assert "timeout" in str(r)


# ── Peer tests ────────────────────────────────────────────────────────────────

class TestPeer:
    def test_str(self, sample_peer: Peer) -> None:
        s = str(sample_peer)
        assert "Bob" in s
        assert "127.0.0.1" in s
        assert "-55" in s


# ── Manager unit tests ────────────────────────────────────────────────────────

class TestWaveShareManagerUnit:
    def test_get_share_intent(self, tmp_file: Path) -> None:
        mgr = WaveShareManager("Alice")
        d = mgr.get_share_intent(tmp_file)
        assert d["filename"] == "test_payload.bin"
        assert d["size"] == 1024 * 1024

    @pytest.mark.asyncio
    async def test_discover_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world")
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "c.txt").write_text("!")

        mgr = WaveShareManager("Alice")
        found = [f async for f in mgr.discover_files(tmp_path, "**/*.txt")]
        assert len(found) == 3

    @pytest.mark.asyncio
    async def test_ble_import_error_without_bleak(self) -> None:
        import waveshare.manager as mgr_mod

        original = mgr_mod._BLEAK_AVAILABLE
        mgr_mod._BLEAK_AVAILABLE = False
        try:
            mgr = WaveShareManager("Alice")
            with pytest.raises(ImportError, match="bleak"):
                await mgr.discover_peers()
        finally:
            mgr_mod._BLEAK_AVAILABLE = original


# ── Wire helper tests ─────────────────────────────────────────────────────────

class TestWireHelpers:
    def test_encode_header_roundtrip(self) -> None:
        import json, struct
        from waveshare.manager import _encode_header, _read_header

        meta = {"filename": "hello.bin", "size": 42, "mimetype": "application/octet-stream"}
        encoded = _encode_header(meta)
        (length,) = struct.unpack(">I", encoded[:4])
        assert length == len(encoded) - 4
        decoded = json.loads(encoded[4:].decode())
        assert decoded == meta


# ── Integration test: loopback send/receive ───────────────────────────────────

class TestLoopbackTransfer:
    """
    Full send→receive cycle over the loopback interface.
    No BLE required — BLE is only used for discovery, not data transfer.
    """

    @pytest.mark.asyncio
    async def test_send_receive_integrity(self, tmp_path: Path) -> None:
        payload = os.urandom(512 * 1024)  # 512 KiB
        src = tmp_path / "source.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "recv"
        dest_dir.mkdir()

        # Build expected digest
        expected_digest = hashlib.sha256(payload).hexdigest()

        async with WaveShareManager("Sender", chunk_size=64 * 1024) as sender:
            # Find the port the sender's server is using
            sender_port = sender._tcp_port
            assert sender_port is not None

            # Manually wire a receiver server on a different port
            recv_mgr = WaveShareManager("Receiver")
            await recv_mgr._start_tcp_server()

            # Run receiver in background
            recv_task = asyncio.create_task(
                recv_mgr.receive_file(dest_dir=dest_dir, progress=False, timeout=30)
            )
            await asyncio.sleep(0.2)  # let server start

            peer = Peer(
                device_id="loopback",
                name="Receiver",
                address="127.0.0.1",
                port=recv_mgr._tcp_port,  # type: ignore[arg-type]
            )

            send_result = await sender.send_file(src, peer, progress=False)
            recv_result = await recv_task

            await recv_mgr.close()

        assert send_result.success
        assert recv_result.success
        assert send_result.checksum == expected_digest
        assert recv_result.checksum == expected_digest

        saved = (dest_dir / "source.bin").read_bytes()
        assert saved == payload

    @pytest.mark.asyncio
    async def test_integrity_error_on_corruption(self, tmp_path: Path) -> None:
        """Simulate bit-flip → IntegrityError should be raised."""
        payload = os.urandom(64 * 1024)
        src = tmp_path / "data.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "recv"
        dest_dir.mkdir()

        # Patch hashlib.sha256 on the SENDER side to return a wrong digest
        bad_hex = "0" * 64

        original_hexdigest = hashlib.sha256(payload).hexdigest()
        assert bad_hex != original_hexdigest  # sanity

        async with WaveShareManager("Sender") as sender:
            recv_mgr = WaveShareManager("Receiver")
            await recv_mgr._start_tcp_server()

            recv_task = asyncio.create_task(
                recv_mgr.receive_file(dest_dir=dest_dir, progress=False, timeout=15)
            )
            await asyncio.sleep(0.2)

            # Monkey-patch the hasher's hexdigest in the sender stream
            real_open = open

            class BadHasher:
                def __init__(self):
                    self._h = hashlib.sha256()
                def update(self, data): self._h.update(data)
                def hexdigest(self): return bad_hex  # lie!

            import builtins
            original_sha256 = hashlib.sha256

            def patched_sha256(*a, **kw):
                return BadHasher()

            hashlib.sha256 = patched_sha256  # type: ignore[assignment]
            try:
                peer = Peer("lo", "Receiver", "127.0.0.1", recv_mgr._tcp_port)  # type: ignore[arg-type]
                with pytest.raises(IntegrityError):
                    await asyncio.gather(
                        sender.send_file(src, peer, progress=False),
                        recv_task,
                        return_exceptions=False,
                    )
            except* IntegrityError:
                pass
            except IntegrityError:
                pass  # expected
            finally:
                hashlib.sha256 = original_sha256  # type: ignore[assignment]
                await recv_mgr.close()
