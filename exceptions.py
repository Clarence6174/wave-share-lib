"""
WaveShare exception hierarchy.
"""


class WaveShareError(Exception):
    """Base exception for all WaveShare errors."""


class PeerNotFoundError(WaveShareError):
    """Raised when no matching BLE peer is discovered within the timeout."""

    def __init__(self, peer_name: str, timeout: float) -> None:
        super().__init__(
            f"Peer {peer_name!r} not found after {timeout:.1f}s of scanning."
        )
        self.peer_name = peer_name
        self.timeout = timeout


class IntegrityError(WaveShareError):
    """Raised when the SHA-256 digest of a received file does not match."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            f"Integrity check failed.\n"
            f"  expected: {expected}\n"
            f"  actual  : {actual}"
        )
        self.expected = expected
        self.actual = actual


class TransferAbortedError(WaveShareError):
    """Raised when a transfer is cancelled mid-stream."""
