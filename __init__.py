"""
WaveShare — Peer-to-peer file transfer library.

BLE discovery + TCP stream + SHA-256 integrity.
"""

from .manager import WaveShareManager
from .models import ShareIntent, TransferResult, Peer
from .exceptions import (
    WaveShareError,
    PeerNotFoundError,
    IntegrityError,
    TransferAbortedError,
)

__version__ = "0.1.0"
__author__ = "WaveShare Contributors"
__license__ = "MIT"

__all__ = [
    "WaveShareManager",
    "ShareIntent",
    "TransferResult",
    "Peer",
    "WaveShareError",
    "PeerNotFoundError",
    "IntegrityError",
    "TransferAbortedError",
]
