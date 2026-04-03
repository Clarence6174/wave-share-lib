# 🌊 WaveShare

> Peer-to-peer file transfer for humans. BLE discovery. TCP speed. Zero cloud.

[![PyPI version](https://img.shields.io/pypi/v/waveshare?color=0ea5e9&style=flat-square)](https://pypi.org/project/waveshare/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-0ea5e9?style=flat-square)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/your-org/waveshare/tests.yml?style=flat-square&label=tests)](https://github.com/your-org/waveshare/actions)
[![Coverage](https://img.shields.io/codecov/c/github/your-org/waveshare?style=flat-square)](https://codecov.io/gh/your-org/waveshare)

WaveShare is an **async Python library** that lets two devices exchange files
directly — no internet, no cloud, no server setup.  
It uses **Bluetooth Low Energy** (BLE) for zero-config peer discovery and a
**direct TCP socket** for full-speed data transfer, wrapped in a clean
`asyncio`-native API with SHA-256 end-to-end integrity verification.

---

## Features

| Capability | Detail |
|---|---|
| 🔵 **BLE discovery** | `bleak`-powered scan — finds peers in seconds |
| ⚡ **Fast TCP stream** | Chunk-based transfer, saturates your local link |
| 🔒 **Integrity check** | SHA-256 digest verified before the receiver confirms |
| 📋 **Share sheet hook** | `get_share_intent()` → drop-in for Android / iOS share menus |
| 🔍 **Async file scan** | Non-blocking directory traversal via `asyncio.to_thread` |
| 📦 **Modern packaging** | `hatchling` + `pyproject.toml`, typed, zero `setup.py` |

---

## Installation

```bash
# Core library (no Bluetooth)
pip install waveshare

# With BLE peer discovery
pip install "waveshare[ble]"
```

**System requirements for BLE:**

| Platform | Requirement |
|---|---|
| Linux | BlueZ ≥ 5.43 (`sudo apt install bluez`) |
| macOS | Core Bluetooth (macOS 10.15+) |
| Windows | WinRT Bluetooth (Windows 10 1809+) |

> **No Bluetooth?** You can still use WaveShare for TCP-only transfers by
> supplying `Peer` objects manually with known IP addresses.

---

## Quick Start

### Send a file

```python
import asyncio
from waveshare import WaveShareManager

async def main():
    async with WaveShareManager("Alice") as mgr:
        # Discover peers on the local network/BLE
        peers = await mgr.discover_peers(timeout=10)
        if not peers:
            print("No peers found.")
            return

        print(f"Found: {peers[0]}")

        # Send with live progress bar
        result = await mgr.send_file("holiday_photos.zip", peers[0])
        print(result)  # ✓ holiday_photos.zip (42,000,000 bytes) @ 18.3 MB/s

asyncio.run(main())
```

### Receive a file

```python
import asyncio
from waveshare import WaveShareManager

async def main():
    async with WaveShareManager("Bob") as mgr:
        print(f"Waiting for files on port {mgr._tcp_port}…")
        result = await mgr.receive_file(dest_dir="~/Downloads")
        print(result)  # ✓ holiday_photos.zip (42,000,000 bytes) @ 18.3 MB/s

asyncio.run(main())
```

### Share sheet integration

```python
from waveshare import WaveShareManager

mgr = WaveShareManager("Alice")
intent = mgr.get_share_intent("report.pdf")
print(intent)
# {
#   'filename': 'report.pdf',
#   'size': 524288,
#   'mimetype': 'application/pdf',
#   'checksum_algorithm': 'sha256'
# }

# Pass `intent` directly to an Android ACTION_SEND or iOS UIActivityViewController
```

### Async file discovery

```python
import asyncio
from waveshare import WaveShareManager

async def main():
    async with WaveShareManager("Alice") as mgr:
        async for file in mgr.discover_files("~/Documents", "**/*.pdf"):
            print(file)

asyncio.run(main())
```

### Wait for a specific peer by name

```python
async with WaveShareManager("Alice") as mgr:
    bob = await mgr.wait_for_peer("Bob", timeout=30)
    result = await mgr.send_file("data.csv", bob)
```

---

## Layer diagram

[`docs/layer.excalidraw`](docs/layer.excalidraw)

### Excalidraw diagram

An interactive architecture diagram is available in the repository:  
📐 [`docs/architecture.excalidraw`](docs/architecture.excalidraw)


### Layer breakdown

**① BLE Peer Discovery**  
`WaveShareManager.discover_peers()` runs a `BleakScanner` listening for
devices that advertise `WAVESHARE_SERVICE_UUID`. The advertisement payload
contains a small JSON blob (`{"ip": "...", "port": N}`) that tells the
sender where to open the TCP connection. BLE is *only* used for discovery —
no file bytes travel over BLE.

**② TCP Handshake**  
Once a `Peer` is known, the sender opens a standard TCP connection
(`asyncio.open_connection`). Both sides exchange a magic prefix
(`WAVESHARE\x00`) and a length-prefixed JSON header containing the
`ShareIntent` metadata (filename, size, MIME type). This allows the receiver
to allocate resources and display a UI prompt before the data starts flowing.

**③ Chunk Stream**  
Files are read and written in 256 KiB frames. Each frame is prefixed with a
4-byte big-endian length. A zero-length frame signals end-of-file. This
framing survives TCP segmentation and makes progress tracking trivial.

**④ SHA-256 Integrity**  
The sender hashes every chunk as it passes through (no extra I/O pass).
After the EOF sentinel, the hex digest is sent to the receiver, who computes
its own digest independently. The receiver replies `OK` (success) or `ER`
(mismatch), at which point the sender raises `IntegrityError`.

---

## API Reference

### `WaveShareManager`

```python
WaveShareManager(
    device_name: str,
    *,
    chunk_size: int = 256 * 1024,   # bytes per chunk
    scan_timeout: float = 30.0,     # BLE scan duration
)
```

 Method | Description 

| `discover_peers(timeout?)` | BLE scan → `List[Peer]` sorted by RSSI |
| `wait_for_peer(name, timeout?)` | Block until named peer appears |
| `get_share_intent(path)` | Share-sheet metadata `dict` |
| `send_file(path, peer, *, progress, on_progress)` | Transmit a file |
| `receive_file(dest_dir, *, progress, on_progress, timeout)` | Accept a file |
| `discover_files(root, pattern)` | Async file discovery generator |

### `Peer`

```python
Peer(device_id, name, address, port, rssi=None)
```

Immutable dataclass. Create manually for TCP-only usage (no BLE required):

```python
from waveshare import Peer
bob = Peer("manual", "Bob", "192.168.1.42", 9000)
```

### Exceptions

| Exception | When raised |

| `PeerNotFoundError` | BLE scan timed out without finding the named peer |
| `IntegrityError` | SHA-256 mismatch between sender and receiver |
| `TransferAbortedError` | Transfer cancelled via the manager's cancel event |

---

## Development

```bash
git clone https://github.com/your-org/waveshare
cd waveshare
pip install -e ".[dev,ble]"

# Run tests
pytest tests/ -v --cov=waveshare

# Lint + format
ruff check src/ && ruff format src/

# Type-check
mypy src/waveshare
```

### Contributing

1. Fork the repo and create a feature branch.
2. Add tests — PRs without tests will not be merged.
3. Sign the [Contributor License Agreement](CLA.md) by commenting on your PR.
4. Open a pull request against `main`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full guide.

---

## Roadmap

- [ ] BLE peripheral mode (advertise from Python)
- [ ] Multi-file / directory transfer with a ZIP stream
- [ ] mDNS / DNS-SD fallback when BLE is unavailable
- [ ] Resume interrupted transfers
- [ ] CLI tool (`waveshare send file.zip Bob`)

---

## License

WaveShare is released under the [MIT License](LICENSE).  
© 2024 WaveShare Contributors.
