"""
app_utils.py — shared helpers for the folder-in / process-each / summary apps.

Any app with the same shape (pick a folder, run a per-file task, report
progress + a summary) can reuse these. The two things that differ per app —
how files are discovered and what happens to each file — are passed in as
``discover`` and ``handle`` callbacks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Sequence, Union

PathLike = Union[str, Path]


# --------------------------------------------------------------------------- #
# Value coercion — UI widgets may hand back strings
# --------------------------------------------------------------------------- #

def as_int(v, default: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


# --------------------------------------------------------------------------- #
# Config resolution — first existing path wins; optional embedded fallback
# --------------------------------------------------------------------------- #

def resolve_config(
    paths: Union[PathLike, Sequence[PathLike]],
    default: Union[dict, None] = None,
) -> Union[str, dict]:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    for p in paths:
        p = Path(p)
        if p.is_file():
            print(f"[config] using: {p}")
            return str(p)
    if default is not None:
        print("[config] no JSON found — using embedded default")
        return default
    raise FileNotFoundError(f"config not found in: {[str(p) for p in paths]}")


# --------------------------------------------------------------------------- #
# Generic batch runner (runs in the worker thread)
# --------------------------------------------------------------------------- #

def run_batch(
    input_dir: str,
    output_dir: str = "",
    *,
    discover: Callable[[Path, bool], Sequence[PathLike]],
    handle: Callable[[Path, Path], int],
    recursive: bool = False,
    progress_callback: Union[Callable[[int], None], None] = None,
    made_label: str = "Outputs",
    beside_label: str = "next to each source image",
) -> str:
    """
    Validate the input, discover items, run ``handle`` on each with progress,
    and return a summary string.

    discover(src, recursive) -> list of file paths
    handle(item, out_root)   -> number of artifacts produced (int)

    ``out_root`` passed to handle is the chosen output folder, or the item's
    own parent folder when no output was selected (write beside each source).
    """
    pct = progress_callback or (lambda p: None)
    pct(5)

    in_val = (input_dir or "").strip()
    if not in_val:
        raise ValueError("No input selected")
    src = Path(in_val)
    if not src.exists():
        raise FileNotFoundError(f"Not found: {src}")

    recursive = as_bool(recursive)
    items: List[Path] = [Path(p) for p in discover(src, recursive)]
    pct(15)
    if not items:
        pct(100)
        return f"No images found in:\n{src}"

    out_val = (output_dir or "").strip()
    out_root = Path(out_val) if out_val else None
    if out_root:
        out_root.mkdir(parents=True, exist_ok=True)

    total = len(items)
    done = made = 0
    failed: List[tuple[str, str]] = []

    for i, item in enumerate(items, start=1):
        root = out_root if out_root else item.parent
        try:
            made += int(handle(item, root) or 0)
            done += 1
        except Exception as e:  # report, keep going
            failed.append((item.name, str(e)))
        pct(min(15 + int(i / total * 80), 95))

    pct(100)

    where = str(out_root) if out_root else beside_label
    lines = [
        f"Processed: {done} / {total} image(s)",
        f"{made_label}: {made}",
        f"Output: {where}",
    ]
    if failed:
        lines.append(f"\nFailed ({len(failed)}):")
        lines.extend(f"  · {name} — {err}" for name, err in failed[:10])
        if len(failed) > 10:
            lines.append(f"  · … and {len(failed) - 10} more")
    return "\n".join(lines)