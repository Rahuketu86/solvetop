"""Shared cgroup/proc readers for solvetop's collector and UI.

Kept in one place because the CPU-rate and network-rate bugs found in the
original bash prototype came from computing deltas against a wrong baseline
(zero on first sample) or against the nominal poll interval instead of the
actual elapsed wall-clock time. Both call sites must use the same fix.
"""
import os

import psutil

NET_IFACE = "eth0"


def read_int(path, default=0):
    try:
        with open(path) as f:
            v = f.read().strip()
            return -1 if v == "max" else int(v)
    except FileNotFoundError:
        return default


def read_cpu_usec():
    try:
        with open("/sys/fs/cgroup/cpu.stat") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except FileNotFoundError:
        pass
    return 0


def net_bytes():
    counters = psutil.net_io_counters(pernic=True).get(NET_IFACE)
    if counters is None:
        return 0, 0
    return counters.bytes_recv, counters.bytes_sent


def human_bytes(n):
    if n is None:
        return "0 B"
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{sign}{int(n)} {unit}" if unit == "B" else f"{sign}{n:.1f} {unit}"
        n /= 1024


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solvetop.db")

# /app/data is a bind-mounted host disk shared across the whole box, so
# `df`/psutil.disk_usage on it reports the *entire host's* usage, not what
# this container actually occupies. Only `du` on the tree gives a real number.
APP_DATA_PATH = "/app/data"


def path_size_bytes(path, timeout=30):
    import subprocess

    try:
        out = subprocess.run(
            ["du", "-sb", path], capture_output=True, text=True, timeout=timeout
        )
        return int(out.stdout.split()[0])
    except Exception:
        return None


def du_scan(path=APP_DATA_PATH, top_n=5, timeout=60):
    """Total size of `path` plus its top_n largest immediate subdirectories.

    A single `du --max-depth=1` pass gives both the grand total (the last
    line, for `path` itself) and per-subdirectory sizes, so this is the same
    cost as a plain `du -sb` — no second scan needed for the folder list.
    Returns (total_bytes, [(name, full_path, bytes), ...]) or (None, []).
    """
    import subprocess

    try:
        out = subprocess.run(
            ["du", "-b", "--max-depth=1", path],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None, []

    total = None
    entries = []
    for line in out.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            size = int(parts[0])
        except ValueError:
            continue
        entry_path = parts[1]
        if entry_path.rstrip("/") == path.rstrip("/"):
            total = size
        else:
            entries.append((os.path.basename(entry_path), entry_path, size))

    entries.sort(key=lambda e: e[2], reverse=True)
    return total, entries[:top_n]
