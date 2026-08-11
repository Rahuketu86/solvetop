#!/usr/bin/env python3
"""solvetop — htop-style live resource monitor for this container.

Polls memory/CPU/network/disk/processes directly (so it works standalone,
even if the collector daemon isn't running) and additionally reads
solvetop.db for sparkline history spanning further back than this
process's own runtime.
"""
import json
import os
import signal
import sqlite3
import subprocess
import time
from collections import deque
from datetime import datetime

import psutil
from textual_plotext import PlotextPlot
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header, Input, Label,
    Sparkline, Static, TabbedContent, TabPane,
)

from cleanup import ASK_FIRST_ITEMS, SAFE_ITEMS, delete_items, item_sizes
from common import (
    APP_DATA_PATH, DB_PATH, du_scan, human_bytes, net_bytes,
    read_cpu_usec, read_int, read_memory_events,
    tmux_pid_session_map, tmux_session_details,
)

HISTORY_LEN = 120
REFRESH_SECONDS = 1.0
SHELL_SENTINEL = "+ New Shell"


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


class KillConfirmScreen(ModalScreen):
    """htop-style kill: pick a signal, or back out. Nothing is signaled
    until one of the signal buttons is explicitly clicked/pressed."""

    CSS = """
    KillConfirmScreen { align: center middle; }
    #dialog { width: 70; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #dialog_buttons { height: auto; margin-top: 1; }
    """

    def __init__(self, pid, name):
        super().__init__()
        self.pid = pid
        self.proc_name = name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Send a signal to PID [b]{self.pid}[/b] ([b]{self.proc_name}[/b])?")
            with Horizontal(id="dialog_buttons"):
                yield Button("SIGTERM", id="term", variant="warning")
                yield Button("SIGKILL", id="kill", variant="error")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "term":
            self.dismiss((self.pid, signal.SIGTERM))
        elif event.button.id == "kill":
            self.dismiss((self.pid, signal.SIGKILL))
        else:
            self.dismiss(None)


class ProcessHistoryScreen(ModalScreen):
    """Detail + recent RSS/CPU history for one process, sourced from the
    collector's process_snapshots table (logged independently of whether
    this UI happens to be open)."""

    CSS = """
    ProcessHistoryScreen { align: center middle; }
    #dialog { width: 90; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #dialog Sparkline { height: 3; margin: 1 0; }
    #dialog_buttons { height: auto; margin-top: 1; }
    """

    def __init__(self, pid, name, username, cmdline, rss, cpu_pct, history):
        super().__init__()
        self.pid = pid
        self.proc_name = name
        self.username = username
        self.cmdline = cmdline
        self.rss = rss
        self.cpu_pct = cpu_pct
        self.history = history  # [(ts, rss, cpu_pct), ...] ascending, may be empty

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[b]PID {self.pid} — {self.proc_name}[/b]  (user: {self.username})")
            yield Label(self.cmdline[:100])
            yield Label(f"Current: {human_bytes(self.rss)} RSS, {self.cpu_pct:.1f}% CPU")
            if self.history:
                span_min = (self.history[-1][0] - self.history[0][0]) / 60
                rss_hist = [h[1] for h in self.history]
                cpu_hist = [h[2] for h in self.history]
                yield Label(f"RSS over last {span_min:.1f} min ({len(self.history)} samples):")
                yield Sparkline(rss_hist, id="hist_rss")
                yield Label(
                    f"  avg {human_bytes(sum(rss_hist) / len(rss_hist))} / "
                    f"peak {human_bytes(max(rss_hist))}"
                )
                yield Label("CPU% over same window:")
                yield Sparkline(cpu_hist, id="hist_cpu")
                yield Label(f"  avg {sum(cpu_hist) / len(cpu_hist):.1f}% / peak {max(cpu_hist):.1f}%")
            else:
                yield Label(
                    "No history yet for this process — the collector logs processes "
                    "every few seconds, so a newly-started one may not have samples yet."
                )
            with Horizontal(id="dialog_buttons"):
                yield Button("Close", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class ProcessNameHistoryScreen(ModalScreen):
    """History grouped by process name instead of PID — a PID is thrown
    away and reassigned every time something restarts, so 'has this kind
    of process been resource-hungry over time' needs to look past PID/
    create_time and group by the name (or cmdline) instead."""

    CSS = """
    ProcessNameHistoryScreen { align: center middle; }
    #dialog { width: 90; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #dialog Sparkline { height: 3; margin: 1 0; }
    #dialog_buttons { height: auto; margin-top: 1; }
    """

    def __init__(self, name, cmdline, history):
        super().__init__()
        self.proc_name = name
        self.cmdline = cmdline
        self.history = history  # [(ts, total_rss, total_cpu_pct, instance_count), ...] ascending

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[b]All processes named '{self.proc_name}'[/b] (combined across restarts/PIDs)")
            yield Label(f"Current row's cmdline: {self.cmdline[:100]}")
            if self.history:
                span_min = (self.history[-1][0] - self.history[0][0]) / 60
                rss_hist = [h[1] for h in self.history]
                cpu_hist = [h[2] for h in self.history]
                counts = [h[3] for h in self.history]
                yield Label(
                    f"Combined RSS over last {span_min:.1f} min "
                    f"({len(self.history)} samples, {max(counts)} concurrent instance(s) max):"
                )
                yield Sparkline(rss_hist, id="hist_rss")
                yield Label(
                    f"  avg {human_bytes(sum(rss_hist) / len(rss_hist))} / "
                    f"peak {human_bytes(max(rss_hist))}"
                )
                yield Label("Combined CPU% over same window:")
                yield Sparkline(cpu_hist, id="hist_cpu")
                yield Label(f"  avg {sum(cpu_hist) / len(cpu_hist):.1f}% / peak {max(cpu_hist):.1f}%")
            else:
                yield Label(f"No history yet for any process named '{self.proc_name}'.")
            with Horizontal(id="dialog_buttons"):
                yield Button("Close", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class LongRangeGraphScreen(ModalScreen):
    """Mem%/CPU% over the full retention window, not just the ~2min the
    in-app sparklines hold. SQL does the downsampling (GROUP BY bucket)
    directly on the collector's own snapshots table.

    Rendered with `textual-plotext` (Braille/block terminal-native line
    charts) instead of rasterizing a matplotlib PNG through a terminal
    image protocol — an image needs Sixel/Kitty support to look good and
    falls back to blocky half-block color squares otherwise (e.g. most
    browser-embedded terminals, which run xterm.js with no Kitty support
    and Sixel usually disabled). Braille characters pack 2x4 sub-cells per
    character, so plotext charts stay sharp in *any* terminal that renders
    Unicode Braille correctly — which is virtually all of them."""

    CSS = """
    LongRangeGraphScreen { align: center middle; }
    #dialog { width: 95%; height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
    #dialog_buttons { height: auto; margin-top: 1; }
    PlotextPlot { height: 1fr; }
    """

    def __init__(self, hours, times, mem_hist, cpu_hist):
        super().__init__()
        self.hours = hours
        self.times = times
        self.mem_hist = mem_hist
        self.cpu_hist = cpu_hist

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                f"[b]Last {self.hours:.0f}h[/b] ({len(self.mem_hist)} buckets) — "
                f"mem avg {sum(self.mem_hist)/len(self.mem_hist):.0f}%/peak {max(self.mem_hist):.0f}%  "
                f"cpu avg {sum(self.cpu_hist)/len(self.cpu_hist):.0f}%/peak {max(self.cpu_hist):.0f}%"
            )
            yield PlotextPlot(id="lr_plot")
            with Horizontal(id="dialog_buttons"):
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        plot = self.query_one("#lr_plot", PlotextPlot)
        n = len(self.times)
        # A handful of evenly-spaced HH:MM labels reads far better than a
        # label per bucket, which would just overlap into illegible noise.
        n_ticks = min(8, n)
        step = max(n // n_ticks, 1)
        tick_idx = list(range(0, n, step))
        tick_labels = [self.times[i].strftime("%H:%M") for i in tick_idx]

        plot.plt.subplots(2, 1)

        top = plot.plt.subplot(1, 1)
        top.plot(range(n), self.mem_hist, color="cyan")
        top.title("Memory % of limit")
        top.xticks(tick_idx, tick_labels)
        top.ylim(0, max(100, max(self.mem_hist) * 1.1))

        bottom = plot.plt.subplot(2, 1)
        bottom.plot(range(n), self.cpu_hist, color="red")
        bottom.title("CPU %")
        bottom.xticks(tick_idx, tick_labels)
        bottom.ylim(0, max(self.cpu_hist) * 1.1 if self.cpu_hist else 100)

        plot.refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class Solvetop(App):
    CSS = """
    #stats { height: auto; }
    .box { border: round $accent; padding: 0 1; width: 1fr; height: auto; }
    #sparks { height: 5; }
    Sparkline { width: 1fr; height: 3; margin: 0 1; }
    #alert_bar { height: auto; padding: 0 1; background: $error; color: $text; display: none; }
    #cleanup_bar { height: auto; padding: 0 1; }
    #cleanup_status { width: 1fr; content-align: left middle; padding: 0 1; }
    #filter_bar { height: auto; padding: 0 1; display: none; }
    #filter_input { width: 1fr; }
    #filter_count { width: auto; content-align: left middle; padding: 0 1; }
    #main_tabs { height: 1fr; }
    #session_table { height: 1fr; }
    #proc_table { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("m", "sort_mem", "Sort: Mem"),
        Binding("c", "sort_cpu", "Sort: CPU"),
        Binding("s", "clean_safe", "Clean Safe"),
        Binding("a", "review_ask_first", "Review&Clean"),
        Binding("k", "kill_process", "Kill"),
        Binding("i", "process_history", "History (PID)"),
        Binding("n", "process_name_history", "History (name)"),
        Binding("g", "long_range_graph", "24h Graph"),
        Binding("slash", "filter_processes", "Filter"),
        Binding("escape", "cancel_filter", "Cancel filter", show=False),
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
        self.last_sessions = []
        self.last_create_times = {}  # pid -> create_time, kept even after a process exits
        self.last_names = {}  # pid -> process name, kept even after a process exits
        self._fresh_disk = None  # (total_bytes, computed_at, top_dirs) set by a manual cleanup rescan
        self.filter_text = ""
        self.last_oom_kill = None  # None sentinel so we don't alert on the very first read
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
        yield Static("", id="alert_bar")
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
        with TabbedContent(id="main_tabs"):
            with TabPane("Processes", id="tab_processes"):
                with Horizontal(id="filter_bar"):
                    yield Input(
                        placeholder="Filter processes... (Enter to apply, Escape to clear)",
                        id="filter_input",
                    )
                    yield Static("", id="filter_count")
                yield DataTable(id="proc_table")
            with TabPane("Sessions", id="tab_sessions"):
                yield Label(
                    "Enter on a session to attach, or '+ New Shell' for a plain shell "
                    "— Ctrl-b d / exit to come back here"
                )
                yield DataTable(id="session_table")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#proc_table", DataTable)
        table.add_columns("PID", "SESSION", "USER", "RSS", "%MEM", "%CPU", "TIME", "COMMAND")
        table.cursor_type = "row"
        table.focus()

        session_table = self.query_one("#session_table", DataTable)
        session_table.add_columns("SESSION", "WINDOWS", "ATTACHED", "RSS", "CPU%")
        session_table.cursor_type = "row"

        self.set_interval(REFRESH_SECONDS, self.refresh_stats)
        self.refresh_stats()

    def action_filter_processes(self):
        self.query_one("#filter_bar").display = True
        self.query_one("#filter_input", Input).focus()

    def action_cancel_filter(self):
        self.filter_text = ""
        self.query_one("#filter_input", Input).value = ""
        self.query_one("#filter_bar").display = False
        self.query_one("#proc_table", DataTable).focus()
        self.refresh_processes()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter_input":
            self.filter_text = event.value
            self.refresh_processes()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter_input":
            self.query_one("#proc_table", DataTable).focus()

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

    def _selected_pid(self):
        table = self.query_one("#proc_table", DataTable)
        row = table.cursor_row
        if not (0 <= row < len(self.last_pids)):
            return None
        return self.last_pids[row]

    def action_kill_process(self):
        pid = self._selected_pid()
        if pid is None:
            self._set_status("No process selected.")
            return
        proc = self.proc_cache.get(pid)
        try:
            name = proc.name() if proc else str(pid)
        except psutil.Error:
            name = str(pid)
        self.push_screen(KillConfirmScreen(pid, name), self._handle_kill_result)

    def _handle_kill_result(self, result):
        if not result:
            self._set_status("Kill cancelled.")
            return
        pid, sig = result
        try:
            os.kill(pid, sig)
            self._set_status(f"Sent {signal.Signals(sig).name} to PID {pid}.")
        except ProcessLookupError:
            self._set_status(f"PID {pid} no longer exists.")
        except PermissionError:
            self._set_status(f"Permission denied signaling PID {pid} (not owned by this user).")

    def action_process_history(self):
        pid = self._selected_pid()
        if pid is None:
            self._set_status("No process selected.")
            return
        proc = self.proc_cache.get(pid)
        try:
            name = proc.name() if proc else "?"
            username = proc.username() if proc else "?"
            cmdline = (" ".join(proc.cmdline()) or name) if proc else "?"
            rss = proc.memory_info().rss if proc else 0
            cpu_pct = proc.cpu_percent(None) if proc else 0.0
        except psutil.Error:
            name, username, cmdline, rss, cpu_pct = "?", "?", "?", 0, 0.0
        create_time = self.last_create_times.get(pid)
        history = self._read_process_history(pid, create_time)
        self.push_screen(ProcessHistoryScreen(pid, name, username, cmdline, rss, cpu_pct, history))

    def _read_process_history(self, pid, create_time, limit=120):
        if create_time is None:
            return []
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT ts, rss, cpu_pct FROM process_snapshots "
                "WHERE pid=? AND create_time=? ORDER BY ts DESC LIMIT ?",
                (pid, create_time, limit),
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            rows = []
        return list(reversed(rows))

    def action_process_name_history(self):
        pid = self._selected_pid()
        if pid is None:
            self._set_status("No process selected.")
            return
        name = self.last_names.get(pid, "?")
        proc = self.proc_cache.get(pid)
        try:
            cmdline = " ".join(proc.cmdline()) or name if proc else name
        except psutil.Error:
            cmdline = name
        history = self._read_process_history_by_name(name)
        self.push_screen(ProcessNameHistoryScreen(name, cmdline, history))

    def _read_process_history_by_name(self, name, limit=120):
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT ts, SUM(rss), SUM(cpu_pct), COUNT(DISTINCT pid) FROM process_snapshots "
                "WHERE name=? GROUP BY ts ORDER BY ts DESC LIMIT ?",
                (name, limit),
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            rows = []
        return list(reversed(rows))

    def action_long_range_graph(self):
        times, mem_hist, cpu_hist = self._read_long_range(hours=24, points=120)
        if not mem_hist:
            self._set_status("No long-range history yet — give the collector a few minutes.")
            return
        # plotext renders as plain text (no rasterize/rescale step like the
        # matplotlib+image-protocol path), so this is fast enough to build
        # synchronously — no worker thread needed.
        self.push_screen(LongRangeGraphScreen(24, times, mem_hist, cpu_hist))

    def _read_long_range(self, hours=24, points=120):
        """SQL-side bucketing over the full retention window — the collector
        may have tens of thousands of rows for 24h at a 3s interval, so this
        averages server-side instead of pulling them all into Python."""
        bucket_seconds = max(int(hours * 3600 / points), 1)
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            rows = conn.execute(
                f"SELECT CAST(ts / {bucket_seconds} AS INT) AS bucket, AVG(ts), "
                f"AVG(mem_current), AVG(mem_max), AVG(cpu_pct) FROM snapshots "
                f"WHERE ts > (strftime('%s','now') - {hours * 3600}) "
                f"GROUP BY bucket ORDER BY bucket"
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            rows = []
        times = [datetime.fromtimestamp(r[1]) for r in rows]
        mem_hist = [(r[2] / r[3] * 100) if r[3] else 0 for r in rows]
        cpu_hist = [max(r[4] or 0, 0) for r in rows]
        return times, mem_hist, cpu_hist

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_clean_safe":
            self.action_clean_safe()
        elif event.button.id == "btn_clean_ask":
            self.action_review_ask_first()

    def _handle_cleanup_result(self, selected):
        if not selected:
            self._set_status("Cleanup cancelled — nothing selected.")
            return
        self._set_status(f"Deleting {len(selected)} item(s)...")
        self.run_worker(
            lambda: self._do_clean(selected), thread=True, exclusive=True, group="cleanup",
        )

    def _set_status(self, text):
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
        self._set_status(summary)
        self.refresh_stats()

    def _check_alerts(self, mem_pct):
        alerts = []
        events = read_memory_events()
        oom_kill = events.get("oom_kill", 0)
        # None sentinel on the first call — otherwise a container that had
        # already OOM-killed something before solvetop started would alert
        # immediately on launch, which is noise rather than a new event.
        if self.last_oom_kill is not None and oom_kill > self.last_oom_kill:
            alerts.append(f"OOM KILL just happened! (count now {oom_kill})")
        self.last_oom_kill = oom_kill

        if mem_pct >= 90:
            alerts.append(f"Memory at {mem_pct:.0f}% of limit!")

        alert_bar = self.query_one("#alert_bar", Static)
        if alerts:
            alert_bar.update("⚠ " + "  |  ".join(alerts))
            alert_bar.display = True
        else:
            alert_bar.display = False

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
        self._check_alerts(mem_pct)

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
        pid_session = tmux_pid_session_map()

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
                self.last_create_times[pid] = proc.create_time()
                self.last_names[pid] = proc.name()
            except psutil.Error:
                continue
            session = pid_session.get(pid, "-")
            rows.append((pid, session, user, mem, cpu, total_time, cmd))

        for pid in list(self.proc_cache):
            if pid not in seen:
                del self.proc_cache[pid]
        # last_create_times deliberately isn't pruned on exit — history for a
        # process that just died is still worth looking at via 'i'.

        # Session totals are computed over *every* matching process, before
        # the filter/top-30 cap below — a session's real footprint shouldn't
        # depend on whether its processes happened to rank in the visible
        # slice of the (separate) process table.
        self._refresh_sessions(rows)

        total_mem = psutil.virtual_memory().total
        key = (lambda r: r[3]) if self.sort_by == "mem" else (lambda r: r[4])
        rows.sort(key=key, reverse=True)

        if self.filter_text:
            needle = self.filter_text.lower()
            rows = [r for r in rows if needle in r[6].lower()]
            self.query_one("#filter_count", Static).update(f"{len(rows)} match(es)")

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
        for pid, session, user, mem, cpu, total_time, cmd in rows:
            mins, secs = divmod(int(total_time), 60)
            table.add_row(
                str(pid), session, user, human_bytes(mem),
                f"{mem / total_mem * 100:.1f}", f"{cpu:.1f}",
                f"{mins}:{secs:02d}", cmd[:60],
            )
        self.last_pids = new_pids

        if new_pids:
            if selected_pid in new_pids:
                table.move_cursor(row=new_pids.index(selected_pid))
            else:
                table.move_cursor(row=min(selected_row, len(new_pids) - 1))

    def _refresh_sessions(self, all_rows):
        totals = {}  # name -> [rss_sum, cpu_sum]
        for pid, session, user, mem, cpu, total_time, cmd in all_rows:
            if session == "-":
                continue
            t = totals.setdefault(session, [0, 0.0])
            t[0] += mem
            t[1] += cpu

        details = {name: (windows, attached) for name, windows, attached in tmux_session_details()}
        names = sorted(details.keys())

        table = self.query_one("#session_table", DataTable)
        selected_row = table.cursor_row
        selected_name = (
            self.last_sessions[selected_row]
            if 0 <= selected_row < len(self.last_sessions)
            else None
        )

        table.clear()
        table.add_row(SHELL_SENTINEL, "-", "-", "-", "-")
        for name in names:
            windows, attached = details[name]
            rss_sum, cpu_sum = totals.get(name, (0, 0.0))
            table.add_row(
                name, windows, "yes" if attached else "no",
                human_bytes(rss_sum), f"{cpu_sum:.1f}",
            )
        self.last_sessions = [SHELL_SENTINEL] + names

        if selected_name in self.last_sessions:
            table.move_cursor(row=self.last_sessions.index(selected_name))
        else:
            table.move_cursor(row=min(max(selected_row, 0), len(self.last_sessions) - 1))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "session_table":
            return
        row = event.cursor_row
        if not (0 <= row < len(self.last_sessions)):
            return
        name = self.last_sessions[row]
        is_shell = name == SHELL_SENTINEL
        try:
            with self.suspend():
                if is_shell:
                    subprocess.run([os.environ.get("SHELL", "/bin/bash")])
                else:
                    subprocess.run(["tmux", "attach-session", "-t", name])
        except SuspendNotSupported:
            what = "open a shell" if is_shell else "attach"
            self._set_status(f"Can't {what} — this terminal doesn't support suspending the app.")
            return
        label = "shell" if is_shell else f"tmux session '{name}'"
        self._set_status(f"Back from {label}.")


if __name__ == "__main__":
    Solvetop().run()
