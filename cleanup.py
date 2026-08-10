"""Disk cleanup actions surfaced as buttons in the solvetop UI.

These specific paths came out of a manual review of /app/data's top
folders (see solvetop review notes) — they are not a general-purpose
"find junk" scanner. SAFE_ITEMS are pure caches that regenerate on their
own (safe to delete without asking); ASK_FIRST_ITEMS are real installs or
model caches where deleting has a cost (re-download, rebuild), so the UI
requires an explicit per-item confirmation before touching them.
"""
import shutil
from pathlib import Path

from common import path_size_bytes

SAFE_ITEMS = [
    ("Jedi autocomplete cache", "/app/data/.cache/jedi"),
    ("pip download cache", "/app/data/.cache/pip"),
    ("micromamba repodata cache", "/app/data/micromamba/pkgs/cache"),
    ("micromamba package download cache", "/app/data/micromamba/pkgs/https"),
    ("Old Claude CLI 2.1.224", "/app/data/.local/share/claude/versions/2.1.224"),
    ("Old Claude CLI 2.1.223", "/app/data/.local/share/claude/versions/2.1.223"),
]

ASK_FIRST_ITEMS = [
    ("HuggingFace model/dataset cache", "/app/data/.cache/huggingface"),
    ("npx codex install", "/app/data/.local/npx-codex"),
    ("octave-build environment", "/app/data/micromamba/envs/octave-build"),
]


def item_sizes(items):
    """[(label, path, size_bytes)] for items that exist; size is None if missing."""
    result = []
    for label, path in items:
        if Path(path).exists():
            result.append((label, path, path_size_bytes(path) or 0))
        else:
            result.append((label, path, None))
    return result


def delete_items(items):
    """Delete each (label, path) that exists.

    Returns (total_freed_bytes, [(label, status_message), ...]).
    """
    total = 0
    log = []
    for label, path in items:
        p = Path(path)
        if not p.exists():
            log.append((label, "not found, skipped"))
            continue
        size = path_size_bytes(str(p)) or 0
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            total += size
            log.append((label, f"freed"))
        except Exception as e:
            log.append((label, f"error: {e}"))
    return total, log
