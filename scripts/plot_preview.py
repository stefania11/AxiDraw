"""Locate the optional AxiDraw preview CLI and parse its offline report."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any


def resolve_axicli(explicit: str | None) -> str:
    for candidate in (explicit, os.environ.get("AXICLI_PATH", "").strip()):
        if not candidate:
            continue
        explicit_path = Path(candidate)
        if explicit_path.exists():
            return str(explicit_path)
        found_explicit = shutil.which(candidate)
        if found_explicit:
            return found_explicit
        raise RuntimeError(f"axicli was not found at {candidate}.")
    found = shutil.which("axicli")
    if found:
        return found
    raise RuntimeError(
        "axicli was not found. Install requirements-plotter.txt, set AXICLI_PATH, or put axicli on PATH."
    )


def parse_preview_output(text: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    patterns = {
        "estimated_print_time": r"Estimated print time:\s*(.+)",
        "path_to_draw_m": r"Length of path to draw:\s*([0-9.]+)\s*m",
        "pen_up_travel_m": r"Pen-up travel distance:\s*([0-9.]+)\s*m",
        "total_movement_m": r"Total movement distance:\s*([0-9.]+)\s*m",
        "estimate_runtime": r"This estimate took\s*(.+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if not match:
            continue
        value = match.group(1).strip()
        metrics[key] = float(value) if value.replace(".", "", 1).isdigit() else value

    warnings: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        if block.strip().startswith(("Note", "Warning")):
            warnings.append(" ".join(block.split()))
    return {"metrics": metrics, "warnings": warnings, "raw_output": text}
