"""
Shared data models for WaveShare.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Peer:
    """Represents a discovered BLE peer."""

    device_id: str          # BLE address / UUID
    name: str               # Advertised display name
    address: str            # IP address for TCP handoff
    port: int               # TCP port for data stream
    rssi: Optional[int] = None  # Signal strength (dBm)

    def __str__(self) -> str:
        rssi_str = f" ({self.rssi} dBm)" if self.rssi is not None else ""
        return f"Peer({self.name!r} @ {self.address}:{self.port}{rssi_str})"


@dataclass
class ShareIntent:
    """
    Metadata dict compatible with standard mobile/desktop share menus.

    Usage
    -----
    >>> intent = manager.get_share_intent("/path/to/photo.jpg")
    >>> print(intent.to_dict())
    {
        'filename': 'photo.jpg',
        'size': 2097152,
        'mimetype': 'image/jpeg',
        'checksum_algorithm': 'sha256',
    }
    """

    path: Path
    filename: str = field(init=False)
    size: int = field(init=False)
    mimetype: str = field(init=False)
    checksum_algorithm: str = "sha256"

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if not self.path.exists():
            raise FileNotFoundError(f"File not found: {self.path}")
        self.filename = self.path.name
        self.size = self.path.stat().st_size
        mime, _ = mimetypes.guess_type(str(self.path))
        self.mimetype = mime or "application/octet-stream"

    def to_dict(self) -> dict:
        """Return share-sheet-compatible metadata dictionary."""
        return {
            "filename": self.filename,
            "size": self.size,
            "mimetype": self.mimetype,
            "checksum_algorithm": self.checksum_algorithm,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ShareIntent(filename={self.filename!r}, "
            f"size={self.size}, mimetype={self.mimetype!r})"
        )


@dataclass
class TransferResult:
    """Outcome of a completed (or failed) file transfer."""

    success: bool
    filename: str
    bytes_transferred: int
    checksum: Optional[str] = None          # hex digest if verified
    error: Optional[str] = None             # human-readable error if failed
    duration_seconds: Optional[float] = None

    @property
    def throughput_mbps(self) -> Optional[float]:
        """Megabytes-per-second, or None if duration is unavailable."""
        if self.duration_seconds and self.duration_seconds > 0:
            return (self.bytes_transferred / self.duration_seconds) / (1024 * 1024)
        return None

    def __str__(self) -> str:
        if self.success:
            tp = (
                f" @ {self.throughput_mbps:.2f} MB/s"
                if self.throughput_mbps is not None
                else ""
            )
            return f"✓ {self.filename} ({self.bytes_transferred:,} bytes){tp}"
        return f"✗ {self.filename} — {self.error}"
