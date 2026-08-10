#!/usr/bin/env python3
"""Render PNG charts from solvetop.db — the long-range companion to the
in-app sparklines, which only ever show the last ~120 samples.

Usage:
  solvetop-plot                       # mem/cpu/net over last 24h -> ./solvetop-plot.png
  solvetop-plot --hours 6 --out x.png
  solvetop-plot --process claude      # also add a panel for that process name
"""
import argparse
import sqlite3
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from common import DB_PATH


def _bucketed(conn, table, select, hours, bucket_seconds, extra_where="", params=()):
    """SQL-side downsampling (GROUP BY bucket) — avoids pulling potentially
    tens of thousands of raw rows into Python just to average them here."""
    since = f"(strftime('%s','now') - {hours * 3600})"
    query = (
        f"SELECT CAST(ts / {bucket_seconds} AS INT) AS bucket, "
        f"AVG(ts), {select} FROM {table} "
        f"WHERE ts > {since} {extra_where} GROUP BY bucket ORDER BY bucket"
    )
    return conn.execute(query, params).fetchall()


def build_figure(hours=24, points=200, process=None, for_terminal=False):
    """Shared by the CLI and the in-app image view — returns a matplotlib
    Figure (caller decides whether to savefig() or rasterize it to a PIL
    image), or None if there's no data yet for the window.

    `for_terminal=True` renders at a higher source DPI with bolder lines and
    larger text: terminal image protocols (Kitty/Sixel, or the half-cell
    Unicode fallback on terminals/multiplexers without real graphics support)
    are working with far fewer effective pixels than a screen, and thin
    matplotlib default lines/labels turn to illegible mush once downscaled
    to a ~100x30-cell widget. A file viewed on an actual monitor doesn't
    have that problem, so the CLI path keeps matplotlib's normal styling.
    """
    bucket_seconds = max(int(hours * 3600 / points), 1)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    sys_rows = _bucketed(
        conn, "snapshots",
        "AVG(mem_current), AVG(mem_max), AVG(cpu_pct), AVG(rx_rate), AVG(tx_rate)",
        hours, bucket_seconds,
    )
    if not sys_rows:
        conn.close()
        return None

    times = [datetime.fromtimestamp(r[1]) for r in sys_rows]
    mem_pct = [(r[2] / r[3] * 100) if r[3] else 0 for r in sys_rows]
    cpu_pct = [max(r[4] or 0, 0) for r in sys_rows]
    rx_kib = [(r[5] or 0) / 1024 for r in sys_rows]
    tx_kib = [(r[6] or 0) / 1024 for r in sys_rows]

    lw = 3.0 if for_terminal else 1.5
    fontsize = 16 if for_terminal else 10
    dpi = 200 if for_terminal else 100

    with plt.rc_context({"font.size": fontsize}):
        n_panels = 3 + (1 if process else 0)
        fig, axes = plt.subplots(
            n_panels, 1, figsize=(11, 2.4 * n_panels), sharex=True, dpi=dpi,
        )

        axes[0].plot(times, mem_pct, color="#4C9AFF", linewidth=lw)
        axes[0].set_ylabel("Mem %")
        axes[0].set_title(f"solvetop — last {hours:.0f}h ({len(sys_rows)} buckets)")

        axes[1].plot(times, cpu_pct, color="#F87168", linewidth=lw)
        axes[1].set_ylabel("CPU %")

        axes[2].plot(times, rx_kib, label="RX", color="#57D9A3", linewidth=lw)
        axes[2].plot(times, tx_kib, label="TX", color="#FFAB00", linewidth=lw)
        axes[2].set_ylabel("KiB/s")
        axes[2].legend(loc="upper right", fontsize=fontsize * 0.8)

        if process:
            proc_rows = _bucketed(
                conn, "process_snapshots", "SUM(rss), SUM(cpu_pct)", hours, bucket_seconds,
                extra_where="AND name = ?", params=(process,),
            )
            if proc_rows:
                p_times = [datetime.fromtimestamp(r[1]) for r in proc_rows]
                p_rss_mib = [(r[2] or 0) / (1024 * 1024) for r in proc_rows]
                axes[3].plot(p_times, p_rss_mib, color="#B584F5", linewidth=lw)
                axes[3].set_ylabel(f"'{process}' RSS (MiB)")
            else:
                axes[3].text(0.5, 0.5, f"No history for '{process}'", ha="center", transform=axes[3].transAxes)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    conn.close()
    return fig


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=24, help="lookback window (default 24)")
    ap.add_argument("--out", default="solvetop-plot.png", help="output PNG path")
    ap.add_argument("--process", help="also chart this process name's combined RSS/CPU")
    ap.add_argument("--points", type=int, default=200, help="target number of x-axis points")
    args = ap.parse_args()

    fig = build_figure(hours=args.hours, points=args.points, process=args.process)
    if fig is None:
        print(f"No data in the last {args.hours}h — is the solvetop-collector session running?")
        return

    fig.savefig(args.out, dpi=120)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
