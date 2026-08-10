#!/usr/bin/env python3
"""solvetop — htop-style live resource monitor for this container.

Polls memory/CPU/network/disk/processes directly (so it works standalone,
even if the collector daemon isn't running) and additionally reads
solvetop.db for sparkline history spanning further back than this
process's own runtime.
"""
import json
import sqlite3
import time
from collections import deque

import psutil
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Label, Sparkline, Static

from cleanup import ASK_FIRST_ITEMS, SAFE_ITEMS, delete_items, item_sizes
from common import APP_DATA_PATH, DB_PATH, du_scan, human_bytes, net_bytes, read_cpu_usec, read_int

HISTORY_LEN = 120
REFRESH_SECONDS = 1.0


class StatBox(Static):
    def __init__(self, title, **kw):
        super().__init__(**kw)
        self.title = title

    def show(self, lines):
        self.update(f"[b]{self.title}[/b]\n" + "\n".join(lines))


class CleanupReviewScreen(ModalScreen):
    """Per-item selection for a cleanup bucket (safe or ask-first) — every
    item gets its own checkbox, so you can always pick exactly which ones
    to delete regardless of which bucket it came from. `default_checked`
    just controls the starting state (all ticked for the safe bucket since
    it's pre-vetted, none ticked for ask-first) — either can be changed
    before confirming."""

    CSS = """
    CleanupReviewScreen { align: center middle; }
    #dialog { width: 78; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #dialog Checkbox { width: 1fr; }
    #dialog_buttons { height: auto; margin-top: 1; }
    """

    def __init__(self, title, items, default_checked):
        super().__init__()
        self.title_text = title
        self.items = items  # [(label, path, size_bytes_or_None)]
        self.default_checked = default_checked

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[b]{self.title_text}[/b] — tick what you want deleted:")
            for i, (label, path, size) in enumerate(self.items):
                size_str = human_bytes(size) if size is not None else "not found"
                checked = self.default_checked and size is not None
                yield Checkbox(f"{label}  [{size_str}]  {path}", value=checked, id=f"chk_{i}")
            with Horizontal(id="dialog_buttons"):
                yield Button("Delete Selected", id="confirm", variant="error")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            selected = [
                (label, path)
                for i, (label, path, size) in enumerate(self.items)
                if size is not None and self.query_one(f"#chk_{i}", Checkbox).value
            ]
            self.dismiss(selected)
        else:
            self.dismiss(None)


class Solvetop(App):
    CSS = """
    #stats { height: auto; }
    .box { border: round $accent; padding: 0 1; width: 1fr; height: auto; }
    #sparks { height: 5; }
    Sparkline { width: 1fr; height: 3; margin: 0 1; }
    #cleanup_bar { height: auto; padding: 0 1; }
    #cleanup_status { width: 1fr; content-align: left middle; padding: 0 1; }
    #proc_table { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("m", "sort_mem", "Sort: Mem"),
        Binding("c", "sort_cpu", "Sort: CPU"),
        Binding("s", "clean_safe", "Clean Safe"),
        Binding("a", "review_ask_first", "Review&Clean"),
        Binding("tab", "focus_next", "Switch panel", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.sort_by = "mem"
        self.proc_cache = {}
        self.mem_hist = deque(maxlen=HISTORY_LEN)
        self.cpu_hist = deque(maxlen=HISTORY_LEN)
        self.tx_hist = deque(maxlen=HISTORY_LEN)
        self.prev_cpu_usec = read_cpu_usec()
        self.prev_ts = time.monotonic()
        self.prev_rx, self.prev_tx = net_bytes()
        self.last_pids = []
        self._fresh_disk = None  # (total_bytes, computed_at, top_dirs) set by a manual cleanup rescan
        self._load_history()

    def _load_history(self):
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT mem_current, mem_max, cpu_pct, tx_rate FROM snapshots "
                "ORDER BY ts DESC LIMIT ?",
                (HISTORY_LEN,),
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            rows = []
        for mem_current, mem_max, cpu_pct, tx_rate in reversed(rows):
            self.mem_hist.append(0 if not mem_max or mem_max <= 0 else mem_current / mem_max * 100)
            self.cpu_hist.append(max(cpu_pct or 0, 0))
            self.tx_hist.append(tx_rate or 0)

    def _read_app_data_usage(self):
        """app_data_bytes is computed by the collector every few minutes (a
        full `du` over /app/data takes ~2.5s), so the UI just reads the
        latest cached value instead of recomputing it every refresh tick."""
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT app_data_bytes, app_data_computed_at, top_dirs_json FROM snapshots "
                "WHERE app_data_bytes IS NOT NULL ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            row = None
        if row:
            total, computed_at, top_dirs_json = row
            try:
                top_dirs = json.loads(top_dirs_json) if top_dirs_json else []
            except ValueError:
                top_dirs = []
        else:
            total, computed_at, top_dirs = None, None, []

        # A manual cleanup rescan is fresher than the DB until the collector's
        # own next scheduled `du` catches up (it writes on its own cadence,
        # independent of the UI), so prefer whichever is newer.
        if self._fresh_disk and (computed_at is None or self._fresh_disk[1] > computed_at):
            total, computed_at, top_dirs = self._fresh_disk

        return total, computed_at, top_dirs

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="stats"):
            yield StatBox("MEMORY", id="mem_box", classes="box")
            yield StatBox("CPU", id="cpu_box", classes="box")
            yield StatBox("NETWORK (eth0)", id="net_box", classes="box")
            yield StatBox("DISK", id="disk_box", classes="box")
            yield StatBox("TOP FOLDERS (/app/data)", id="topdirs_box", classes="box")
        with Horizontal(id="sparks"):
            yield Sparkline([], id="mem_spark")
            yield Sparkline([], id="cpu_spark")
            yield Sparkline([], id="net_spark")
        with Horizontal(id="cleanup_bar"):
            yield Button("Clean Safe Cache ('s')", id="btn_clean_safe", variant="success")
            yield Button("Review & Clean ('a')", id="btn_clean_ask", variant="warning")
            yield Static("", id="cleanup_status")
        yield DataTable(id="proc_table")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#proc_table", DataTable)
        table.add_columns("PID", "USER", "RSS", "%MEM", "%CPU", "TIME", "COMMAND")
        table.cursor_type = "row"
        table.focus()

        self.set_interval(REFRESH_SECONDS, self.refresh_stats)
        self.refresh_stats()

    def action_sort_mem(self):
        self.sort_by = "mem"

    def action_sort_cpu(self):
        self.sort_by = "cpu"

    def action_clean_safe(self):
        items = item_sizes(SAFE_ITEMS)
        self.push_screen(
            CleanupReviewScreen("Clean Safe Cache", items, default_checked=True),
            self._handle_cleanup_result,
        )

    def action_review_ask_first(self):
        items = item_sizes(ASK_FIRST_ITEMS)
        self.push_screen(
            CleanupReviewScreen("Review & Clean (Ask First)", items, default_checked=False),
            self._handle_cleanup_result,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_clean_safe":
            self.action_clean_safe()
        elif event.button.id == "btn_clean_ask":
            self.action_review_ask_first()

    def _handle_cleanup_result(self, selected):
        if not selected:
            self._set_cleanup_status("Cleanup cancelled — nothing selected.")
            return
        self._set_cleanup_status(f"Deleting {len(selected)} item(s)...")
        self.run_worker(
            lambda: self._do_clean(selected), thread=True, exclusive=True, group="cleanup",
        )

    def _set_cleanup_status(self, text):
        self.query_one("#cleanup_status", Static).update(text)

    def _do_clean(self, items):
        """Runs in a worker thread — deletes files/rescans disk usage, both
        of which block for real I/O, so this must stay off the UI thread."""
        total_freed, log = delete_items(items)
        new_total, new_top_dirs = du_scan(APP_DATA_PATH)
        self.call_from_thread(self._cleanup_done, total_freed, log, new_total, new_top_dirs)

    def _cleanup_done(self, total_freed, log, new_total, new_top_dirs):
        if new_total is not None:
            self._fresh_disk = (new_total, time.time(), new_top_dirs)
        failed = [f"{label}: {status}" for label, status in log if status.startswith("error")]
        summary = f"Freed {human_bytes(total_freed)}."
        if failed:
            summary += f" {len(failed)} failed: {'; '.join(failed)}"
        self._set_cleanup_status(summary)
        self.refresh_stats()

    def refresh_stats(self):
        mem_current = read_int("/sys/fs/cgroup/memory.current")
        mem_peak = read_int("/sys/fs/cgroup/memory.peak")
        mem_max = read_int("/sys/fs/cgroup/memory.max")
        swap_current = read_int("/sys/fs/cgroup/memory.swap.current")
        pids = read_int("/sys/fs/cgroup/pids.current")

        now = time.monotonic()
        elapsed = max(now - self.prev_ts, 1e-6)
        self.prev_ts = now

        cur_cpu = read_cpu_usec()
        cpu_pct = ((cur_cpu - self.prev_cpu_usec) / 1_000_000) / elapsed * 100
        self.prev_cpu_usec = cur_cpu

        rx, tx = net_bytes()
        rx_rate = (rx - self.prev_rx) / elapsed
        tx_rate = (tx - self.prev_tx) / elapsed
        self.prev_rx, self.prev_tx = rx, tx

        mem_pct = 0 if mem_max <= 0 else mem_current / mem_max * 100

        self.mem_hist.append(mem_pct)
        self.cpu_hist.append(max(cpu_pct, 0))
        self.tx_hist.append(tx_rate)

        self.query_one("#mem_box", StatBox).show([
            f"Current: {human_bytes(mem_current)} ({mem_pct:.0f}%)",
            f"Peak:    {human_bytes(mem_peak)}",
            f"Limit:   {'unlimited' if mem_max < 0 else human_bytes(mem_max)}",
            f"Swap:    {human_bytes(swap_current)}",
        ])
        self.query_one("#cpu_box", StatBox).show([
            f"Usage: {max(cpu_pct, 0):.1f}%",
            f"Total: {cur_cpu / 1_000_000:.1f}s",
            f"PIDs:  {pids}",
        ])
        self.query_one("#net_box", StatBox).show([
            f"RX: {human_bytes(rx_rate)}/s  (total {human_bytes(rx)})",
            f"TX: {human_bytes(tx_rate)}/s  (total {human_bytes(tx)})",
        ])
        app_data_bytes, app_data_ts, top_dirs = self._read_app_data_usage()
        host_disk = psutil.disk_usage(APP_DATA_PATH)
        if app_data_bytes is None:
            app_data_line = "/app/data: computing... (collector starting)"
        else:
            age = max(time.time() - app_data_ts, 0)
            app_data_line = f"/app/data: {human_bytes(app_data_bytes)}  (as of {int(age)}s ago)"
        self.query_one("#disk_box", StatBox).show([
            app_data_line,
            f"Host disk (shared): {host_disk.percent:.0f}% used, {human_bytes(host_disk.free)} free",
        ])

        if top_dirs:
            topdirs_lines = [f"{name:<20} {human_bytes(size)}" for name, _path, size in top_dirs]
        else:
            topdirs_lines = ["computing... (collector starting)"]
        self.query_one("#topdirs_box", StatBox).show(topdirs_lines)

        self.query_one("#mem_spark", Sparkline).data = list(self.mem_hist)
        self.query_one("#cpu_spark", Sparkline).data = list(self.cpu_hist)
        self.query_one("#net_spark", Sparkline).data = list(self.tx_hist)

        self.refresh_processes()

    def refresh_processes(self):
        rows = []
        seen = set()
        for p in psutil.process_iter(["pid"]):
            pid = p.info["pid"]
            seen.add(pid)
            proc = self.proc_cache.get(pid)
            if proc is None:
                proc = p
                try:
                    proc.cpu_percent(None)
                except psutil.Error:
                    continue
                self.proc_cache[pid] = proc
            try:
                mem = proc.memory_info().rss
                cpu = proc.cpu_percent(None)
                user = proc.username()
                cmd = " ".join(proc.cmdline()) or proc.name()
                cpu_times = proc.cpu_times()
                total_time = cpu_times.user + cpu_times.system
            except psutil.Error:
                continue
            rows.append((pid, user, mem, cpu, total_time, cmd))

        for pid in list(self.proc_cache):
            if pid not in seen:
                del self.proc_cache[pid]

        total_mem = psutil.virtual_memory().total
        key = (lambda r: r[2]) if self.sort_by == "mem" else (lambda r: r[3])
        rows.sort(key=key, reverse=True)
        rows = rows[:30]

        table = self.query_one("#proc_table", DataTable)

        # table.clear() resets cursor_row to 0, which is why the selection
        # kept jumping to the top row on every refresh. Remember which PID
        # was selected and re-find it in the freshly sorted rows so the
        # cursor stays on the same process even as its rank shifts.
        selected_row = table.cursor_row
        selected_pid = (
            self.last_pids[selected_row]
            if 0 <= selected_row < len(self.last_pids)
            else None
        )

        table.clear()
        new_pids = [r[0] for r in rows]
        for pid, user, mem, cpu, total_time, cmd in rows:
            mins, secs = divmod(int(total_time), 60)
            table.add_row(
                str(pid), user, human_bytes(mem),
                f"{mem / total_mem * 100:.1f}", f"{cpu:.1f}",
                f"{mins}:{secs:02d}", cmd[:60],
            )
        self.last_pids = new_pids

        if new_pids:
            if selected_pid in new_pids:
                table.move_cursor(row=new_pids.index(selected_pid))
            else:
                table.move_cursor(row=min(selected_row, len(new_pids) - 1))


if __name__ == "__main__":
    Solvetop().run()
