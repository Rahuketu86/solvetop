#!/usr/bin/env python3
"""solvetop background collector.

Polls container-scoped memory (cgroup v2), CPU (cgroup v2), network (eth0,
this container's own veth — see solvetop review notes), disk, and PID count
every SOLVETOP_INTERVAL seconds and appends a row to solvetop.db so the
`solvetop` TUI can show sparkline history even on a fresh launch.

Meant to run as a long-lived background process (started via the `sessions`
startup mechanism), independent of whether the TUI is currently attached.
"""
import json
import os
import signal
import sqlite3
import time

import psutil

from common import APP_DATA_PATH, DB_PATH, du_scan, read_cpu_usec, read_int, net_bytes

INTERVAL = float(os.environ.get("SOLVETOP_INTERVAL", 3))
RETENTION_SECONDS = 24 * 3600
# du -sb over /app/data takes ~2.5s (300k+ files) — far too expensive to run
# every INTERVAL tick, so it's refreshed on its own, much slower cadence.
DU_INTERVAL = float(os.environ.get("SOLVETOP_DU_INTERVAL", 300))
# Logging every process every INTERVAL (3s) would be ~75 rows x 1200/hour —
# a coarser cadence keeps process_snapshots bounded while still giving a
# usable trend when plotted later.
PROC_LOG_INTERVAL = float(os.environ.get("SOLVETOP_PROC_LOG_INTERVAL", 10))

_running = True


def _stop(signum, frame):
    global _running
    _running = False


def init_db(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            ts REAL PRIMARY KEY,
            mem_current INTEGER,
            mem_peak INTEGER,
            mem_max INTEGER,
            swap_current INTEGER,
            cpu_usec INTEGER,
            cpu_pct REAL,
            pids INTEGER,
            rx_bytes INTEGER,
            tx_bytes INTEGER,
            rx_rate REAL,
            tx_rate REAL,
            host_disk_pct REAL,
            app_data_bytes INTEGER,
            app_data_computed_at REAL,
            top_dirs_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS process_snapshots (
            ts REAL,
            pid INTEGER,
            create_time REAL,
            name TEXT,
            cmdline TEXT,
            username TEXT,
            rss INTEGER,
            cpu_pct REAL,
            PRIMARY KEY (ts, pid)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proc_pid_ct ON process_snapshots(pid, create_time, ts)"
    )
    conn.commit()


def collect_processes(proc_cache):
    """Snapshot every process's RSS/CPU. `proc_cache` (pid -> psutil.Process)
    is caller-owned and persists across calls — psutil.cpu_percent() only
    returns a real value on the *second* call for a given Process object, so
    reusing the same objects each cycle is what makes cpu_pct meaningful."""
    rows = []
    seen = set()
    for p in psutil.process_iter(["pid"]):
        pid = p.info["pid"]
        seen.add(pid)
        proc = proc_cache.get(pid)
        if proc is None:
            proc = p
            try:
                proc.cpu_percent(None)
            except psutil.Error:
                continue
            proc_cache[pid] = proc
        try:
            rows.append((
                pid, proc.create_time(), proc.name(),
                " ".join(proc.cmdline()) or proc.name(),
                proc.username(), proc.memory_info().rss, proc.cpu_percent(None),
            ))
        except psutil.Error:
            continue
    for pid in list(proc_cache):
        if pid not in seen:
            del proc_cache[pid]
    return rows


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    prev_cpu = read_cpu_usec()
    prev_rx, prev_tx = net_bytes()
    prev_ts = time.monotonic()

    last_du_bytes, last_top_dirs = du_scan(APP_DATA_PATH)
    last_du_ts = time.monotonic()
    last_du_wall_ts = time.time()

    proc_cache = {}
    last_proc_log_ts = 0.0  # forces a log on the very first tick

    while _running:
        time.sleep(INTERVAL)
        now = time.monotonic()
        elapsed = max(now - prev_ts, 1e-6)
        prev_ts = now

        mem_current = read_int("/sys/fs/cgroup/memory.current")
        mem_peak = read_int("/sys/fs/cgroup/memory.peak")
        mem_max = read_int("/sys/fs/cgroup/memory.max")
        swap_current = read_int("/sys/fs/cgroup/memory.swap.current")
        pids = read_int("/sys/fs/cgroup/pids.current")

        cur_cpu = read_cpu_usec()
        cpu_pct = ((cur_cpu - prev_cpu) / 1_000_000) / elapsed * 100
        prev_cpu = cur_cpu

        rx, tx = net_bytes()
        rx_rate = (rx - prev_rx) / elapsed
        tx_rate = (tx - prev_tx) / elapsed
        prev_rx, prev_tx = rx, tx

        host_disk_pct = psutil.disk_usage(APP_DATA_PATH).percent

        if now - last_du_ts >= DU_INTERVAL:
            du, top_dirs = du_scan(APP_DATA_PATH)
            if du is not None:
                last_du_bytes = du
                last_top_dirs = top_dirs
            last_du_ts = time.monotonic()
            last_du_wall_ts = time.time()

        conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(), mem_current, mem_peak, mem_max, swap_current,
                cur_cpu, cpu_pct, pids, rx, tx, rx_rate, tx_rate,
                host_disk_pct, last_du_bytes, last_du_wall_ts,
                json.dumps(last_top_dirs),
            ),
        )
        conn.execute(
            "DELETE FROM snapshots WHERE ts < ?", (time.time() - RETENTION_SECONDS,)
        )

        if now - last_proc_log_ts >= PROC_LOG_INTERVAL:
            last_proc_log_ts = now
            proc_ts = time.time()
            proc_rows = collect_processes(proc_cache)
            conn.executemany(
                "INSERT OR REPLACE INTO process_snapshots VALUES (?,?,?,?,?,?,?,?)",
                [(proc_ts, pid, ct, name, cmd, user, rss, cpu)
                 for pid, ct, name, cmd, user, rss, cpu in proc_rows],
            )
            conn.execute(
                "DELETE FROM process_snapshots WHERE ts < ?", (time.time() - RETENTION_SECONDS,)
            )

        conn.commit()

    conn.close()


if __name__ == "__main__":
    main()
