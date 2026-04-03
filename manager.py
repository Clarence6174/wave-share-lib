"""
WaveShareManager — the heart of the library.

Architecture
============

    ┌─────────────────────────────────────────────────────┐
    │                  WaveShareManager                    │
    │                                                     │
    │  ① BLE scan (bleak)  →  discover Peers              │
    │  ② TCP handshake     →  negotiate port + metadata   │
    │  ③ Chunk stream      →  asyncio StreamWriter        │
    │  ④ SHA-256 verify    →  hashlib digest              │
    └─────────────────────────────────────────────────────┘

BLE is used **only** for peer advertisement / discovery.
Actual file bytes travel over a direct TCP socket for throughput.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import struct
import time
from pathlib import Path
from typing import AsyncIterator, Callable, List, Optional, Sequence

from tqdm import tqdm

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.device import BLEDevice
    _BLEAK_AVAILABLE = True
except ImportError:  # pragma: no cover
    _BLEAK_AVAILABLE = False

from .exceptions import IntegrityError, PeerNotFoundError, TransferAbortedError
from .models import Peer, ShareIntent, TransferResult

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Custom BLE service UUID advertised by WaveShare instances.
WAVESHARE_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

# Each chunk is 256 KiB — balances memory pressure and syscall overhead.
DEFAULT_CHUNK_SIZE: int = 256 * 1024  # 256 KiB

# Maximum seconds to wait for a BLE peer advertisement.
DEFAULT_SCAN_TIMEOUT: float = 30.0

# TCP server listen backlog.
_TCP_BACKLOG: int = 5

# Wire-protocol magic prefix so both sides can validate the handshake.
_MAGIC = b"WAVESHARE\x00"


# ── Wire helpers ───────────────────────────────────────────────────────────────

def _encode_header(meta: dict) -> bytes:
    """Serialise metadata as length-prefixed JSON."""
    payload = json.dumps(meta).encode()
    return struct.pack(">I", len(payload)) + payload


async def _read_header(reader: asyncio.StreamReader) -> dict:
    """Read and deserialise the length-prefixed JSON header."""
    raw_len = await reader.readexactly(4)
    (length,) = struct.unpack(">I", raw_len)
    raw = await reader.readexactly(length)
    return json.loads(raw.decode())


# ── Manager ────────────────────────────────────────────────────────────────────

class WaveShareManager:
    """
    High-level async API for peer discovery and file transfer.

    Parameters
    ----------
    device_name:
        Human-readable name this node advertises over BLE.
    chunk_size:
        Transfer chunk size in bytes (default 256 KiB).
    scan_timeout:
        Seconds to scan for BLE peers before raising ``PeerNotFoundError``.

    Examples
    --------
    **Send a file**::

        async with WaveShareManager("Alice") as mgr:
            peers = await mgr.discover_peers(timeout=10)
            result = await mgr.send_file("photo.jpg", peers[0])
            print(result)

    **Receive a file**::

        async with WaveShareManager("Bob") as mgr:
            result = await mgr.receive_file(dest_dir="~/Downloads")
            print(result)
    """

    def __init__(
        self,
        device_name: str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        scan_timeout: float = DEFAULT_SCAN_TIMEOUT,
    ) -> None:
        self.device_name = device_name
        self.chunk_size = chunk_size
        self.scan_timeout = scan_timeout

        self._server: Optional[asyncio.Server] = None
        self._tcp_port: Optional[int] = None
        self._cancel_event: asyncio.Event = asyncio.Event()

    # ── Context manager ────────────────────────────────────────────────────────

    async def __aenter__(self) -> "WaveShareManager":
        await self._start_tcp_server()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def close(self) -> None:
        """Gracefully shut down the TCP listener."""
        self._cancel_event.set()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.debug("TCP server closed.")

    # ── TCP server ─────────────────────────────────────────────────────────────

    async def _start_tcp_server(self) -> None:
        """Bind an ephemeral TCP port for incoming transfers."""
        self._server = await asyncio.start_server(
            self._noop_handler,   # replaced per-transfer
            host="0.0.0.0",
            backlog=_TCP_BACKLOG,
        )
        # Retrieve the port the OS assigned.
        sock = self._server.sockets[0]
        self._tcp_port = sock.getsockname()[1]
        logger.info("WaveShare TCP listener on port %d", self._tcp_port)

    async def _noop_handler(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:  # pragma: no cover
        writer.close()

    def _local_ip(self) -> str:
        """Best-effort detection of the local LAN IP address."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            except OSError:
                return "127.0.0.1"

    # ── BLE discovery ──────────────────────────────────────────────────────────

    async def discover_peers(
        self,
        timeout: Optional[float] = None,
    ) -> List[Peer]:
        """
        Scan for nearby WaveShare peers via BLE advertisement.

        Returns a list of :class:`~waveshare.models.Peer` objects sorted by
        signal strength (strongest first).  Requires *bleak* and a system
        Bluetooth adapter.

        Parameters
        ----------
        timeout:
            Override the instance-level ``scan_timeout``.

        Raises
        ------
        ImportError
            If *bleak* is not installed.
        """
        if not _BLEAK_AVAILABLE:
            raise ImportError(
                "bleak is required for BLE discovery. "
                "Install it with: pip install waveshare[ble]"
            )

        timeout = timeout or self.scan_timeout
        logger.info("Scanning for WaveShare peers (%.1fs)…", timeout)

        discovered: List[Peer] = []

        def _callback(device: "BLEDevice", advertisement_data) -> None:  # type: ignore[type-arg]
            uuids = [str(u).lower() for u in (advertisement_data.service_uuids or [])]
            if WAVESHARE_SERVICE_UUID.lower() not in uuids:
                return

            # The service data encodes JSON: {"ip": "...", "port": N}
            svc_data = advertisement_data.service_data or {}
            raw = svc_data.get(WAVESHARE_SERVICE_UUID.lower(), b"{}")
            try:
                meta = json.loads(raw)
            except Exception:
                meta = {}

            peer = Peer(
                device_id=device.address,
                name=device.name or "Unknown",
                address=meta.get("ip", device.address),
                port=meta.get("port", 9000),
                rssi=advertisement_data.rssi,
            )
            if not any(p.device_id == peer.device_id for p in discovered):
                discovered.append(peer)
                logger.debug("Found peer: %s", peer)

        async with BleakScanner(detection_callback=_callback) as _scanner:
            await asyncio.sleep(timeout)

        discovered.sort(key=lambda p: p.rssi or -999, reverse=True)
        logger.info("Discovery complete — %d peer(s) found.", len(discovered))
        return discovered

    async def wait_for_peer(
        self,
        peer_name: str,
        timeout: Optional[float] = None,
    ) -> Peer:
        """
        Block until a peer with the given *display name* is discovered.

        Raises
        ------
        PeerNotFoundError
            If the peer does not appear within *timeout* seconds.
        """
        timeout = timeout or self.scan_timeout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            peers = await self.discover_peers(timeout=min(5.0, deadline - time.monotonic()))
            for p in peers:
                if p.name.lower() == peer_name.lower():
                    return p
        raise PeerNotFoundError(peer_name, timeout)

    # ── Share Intent ───────────────────────────────────────────────────────────

    def get_share_intent(self, file_path: str | Path) -> dict:
        """
        Return a share-sheet-compatible metadata dictionary.

        Compatible with Android ``ACTION_SEND`` intents and iOS
        ``UIActivityViewController`` item providers.

        Parameters
        ----------
        file_path:
            Absolute or relative path to the file to share.

        Returns
        -------
        dict
            Keys: ``filename``, ``size``, ``mimetype``,
            ``checksum_algorithm``.

        Example
        -------
        >>> mgr = WaveShareManager("Alice")
        >>> mgr.get_share_intent("report.pdf")
        {'filename': 'report.pdf', 'size': 512000,
         'mimetype': 'application/pdf', 'checksum_algorithm': 'sha256'}
        """
        return ShareIntent(file_path).to_dict()

    # ── File send ──────────────────────────────────────────────────────────────

    async def send_file(
        self,
        file_path: str | Path,
        peer: Peer,
        *,
        progress: bool = True,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> TransferResult:
        """
        Send a file to *peer* over TCP with SHA-256 integrity check.

        Parameters
        ----------
        file_path:
            Path to the local file to transmit.
        peer:
            Destination :class:`~waveshare.models.Peer`.
        progress:
            Display a tqdm progress bar (default ``True``).
        on_progress:
            Optional callback ``(bytes_sent, total_bytes) -> None``.

        Returns
        -------
        TransferResult
        """
        path = Path(file_path)
        intent = ShareIntent(path)
        total = intent.size
        start = time.monotonic()

        logger.info("Connecting to %s …", peer)
        reader, writer = await asyncio.open_connection(peer.address, peer.port)

        try:
            # ── Handshake ────────────────────────────────────────────────────
            writer.write(_MAGIC)
            writer.write(_encode_header(intent.to_dict()))
            await writer.drain()

            ack = await reader.readexactly(len(_MAGIC))
            if ack != _MAGIC:
                raise WaveShareError("Invalid handshake from receiver.")  # type: ignore[name-defined]

            # ── Stream chunks ────────────────────────────────────────────────
            hasher = hashlib.sha256()
            sent = 0

            bar = tqdm(
                total=total,
                unit="B",
                unit_scale=True,
                desc=intent.filename,
                disable=not progress,
            )

            with open(path, "rb") as fh, bar:
                while True:
                    if self._cancel_event.is_set():
                        raise TransferAbortedError("Transfer cancelled by caller.")
                    chunk = fh.read(self.chunk_size)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    writer.write(struct.pack(">I", len(chunk)))
                    writer.write(chunk)
                    await writer.drain()
                    sent += len(chunk)
                    bar.update(len(chunk))
                    if on_progress:
                        on_progress(sent, total)

            # EOF sentinel
            writer.write(struct.pack(">I", 0))
            await writer.drain()

            # ── Send digest ──────────────────────────────────────────────────
            digest = hasher.hexdigest()
            writer.write(digest.encode())
            await writer.drain()

            # ── Await verification ───────────────────────────────────────────
            verdict = await reader.readexactly(2)
            duration = time.monotonic() - start

            if verdict == b"OK":
                logger.info("Transfer complete: %s (%d bytes)", intent.filename, sent)
                return TransferResult(
                    success=True,
                    filename=intent.filename,
                    bytes_transferred=sent,
                    checksum=digest,
                    duration_seconds=duration,
                )
            else:
                remote_digest = (await reader.read(64)).decode(errors="replace")
                raise IntegrityError(digest, remote_digest)

        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    # ── File receive ───────────────────────────────────────────────────────────

    async def receive_file(
        self,
        dest_dir: str | Path = ".",
        *,
        progress: bool = True,
        on_progress: Optional[Callable[[int, int], None]] = None,
        timeout: Optional[float] = 120.0,
    ) -> TransferResult:
        """
        Wait for an incoming file transfer and save it to *dest_dir*.

        The TCP listener started in ``__aenter__`` is reused.

        Parameters
        ----------
        dest_dir:
            Directory in which to save the received file.
        progress:
            Display a tqdm progress bar (default ``True``).
        on_progress:
            Optional callback ``(bytes_received, total_bytes) -> None``.
        timeout:
            Seconds to wait for an incoming connection.

        Returns
        -------
        TransferResult

        Raises
        ------
        asyncio.TimeoutError
            If no sender connects within *timeout* seconds.
        IntegrityError
            If the received file's digest does not match the sender's.
        """
        if self._server is None:
            raise RuntimeError(
                "Call `async with WaveShareManager(...) as mgr:` before receive_file()."
            )

        dest_dir = Path(dest_dir).expanduser().resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)

        result_future: asyncio.Future[TransferResult] = asyncio.get_event_loop().create_future()

        async def _handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            start = time.monotonic()
            try:
                magic = await reader.readexactly(len(_MAGIC))
                if magic != _MAGIC:
                    writer.close()
                    return

                meta = await _read_header(reader)
                filename: str = meta["filename"]
                total: int = meta["size"]
                expected_algo: str = meta.get("checksum_algorithm", "sha256")

                # ACK
                writer.write(_MAGIC)
                await writer.drain()

                hasher = hashlib.new(expected_algo)
                received = 0
                dest_path = dest_dir / filename

                bar = tqdm(
                    total=total,
                    unit="B",
                    unit_scale=True,
                    desc=filename,
                    disable=not progress,
                )

                with open(dest_path, "wb") as fh, bar:
                    while True:
                        raw_len = await reader.readexactly(4)
                        (chunk_len,) = struct.unpack(">I", raw_len)
                        if chunk_len == 0:
                            break
                        chunk = await reader.readexactly(chunk_len)
                        hasher.update(chunk)
                        fh.write(chunk)
                        received += chunk_len
                        bar.update(chunk_len)
                        if on_progress:
                            on_progress(received, total)

                sender_digest = (await reader.read(64)).decode(errors="replace").strip()
                local_digest = hasher.hexdigest()

                duration = time.monotonic() - start

                if local_digest == sender_digest:
                    writer.write(b"OK")
                    await writer.drain()
                    result_future.set_result(
                        TransferResult(
                            success=True,
                            filename=filename,
                            bytes_transferred=received,
                            checksum=local_digest,
                            duration_seconds=duration,
                        )
                    )
                else:
                    writer.write(b"ER")
                    writer.write(local_digest.encode())
                    await writer.drain()
                    result_future.set_exception(
                        IntegrityError(sender_digest, local_digest)
                    )
            except Exception as exc:
                if not result_future.done():
                    result_future.set_exception(exc)
            finally:
                writer.close()

        # Temporarily override the server handler.
        self._server._protocol_factory = lambda: asyncio.StreamReaderProtocol(  # type: ignore[attr-defined]
            asyncio.StreamReader(), _handler
        )

        # Re-create a fresh server pointing at the handler.
        port = self._tcp_port
        srv = await asyncio.start_server(_handler, host="0.0.0.0", port=port)
        try:
            return await asyncio.wait_for(result_future, timeout=timeout)
        finally:
            srv.close()
            await srv.wait_closed()

    # ── Async file discovery ───────────────────────────────────────────────────

    async def discover_files(
        self,
        root: str | Path,
        pattern: str = "**/*",
    ) -> AsyncIterator[Path]:
        """
        Non-blocking recursive file discovery using ``asyncio.to_thread``.

        Yields :class:`pathlib.Path` objects for every matching file so the
        event loop remains responsive during large directory traversals.

        Parameters
        ----------
        root:
            Directory to search.
        pattern:
            Glob pattern (default ``**/*`` — all files recursively).

        Example
        -------
        ::

            async for file in mgr.discover_files("~/Documents", "**/*.pdf"):
                print(file)
        """
        root = Path(root).expanduser().resolve()

        def _scan() -> list[Path]:
            return [p for p in root.glob(pattern) if p.is_file()]

        files = await asyncio.to_thread(_scan)
        for f in files:
            yield f
