"""Sanitize drawing plans, measure paths, and render A4 SVG geometry."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PAGE_WIDTH_MM = 297.0
PAGE_HEIGHT_MM = 210.0
MAX_COMMANDS = 80
MAX_POINTS_PER_POLYLINE = 80
ALLOWED_COMMANDS = {"line", "polyline", "circle", "text"}

GLYPHS: dict[str, list[str]] = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10011", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "10010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
    "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ">": ["10000", "01000", "00100", "00010", "00100", "01000", "10000"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
}


@dataclass(frozen=True)
class SanitizedPlan:
    title: str
    description: str
    commands: list[dict[str, Any]]
    warnings: list[str]
    source_model: str = "offline"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _number(raw: Any, default: float = 0.0) -> float:
    if isinstance(raw, bool):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _point(raw: Any, warnings: list[str], label: str) -> list[float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        warnings.append(f"dropped malformed point for {label}")
        return None
    x = _clamp(_number(raw[0]), 0.0, PAGE_WIDTH_MM)
    y = _clamp(_number(raw[1]), 0.0, PAGE_HEIGHT_MM)
    return [round(x, 3), round(y, 3)]


def _clean_title(raw: Any) -> str:
    title = str(raw or "axidraw_demo").strip().lower()
    title = re.sub(r"[^a-z0-9_ -]+", "", title)
    title = re.sub(r"[\s-]+", "_", title).strip("_")
    return title[:64] or "axidraw_demo"


def sanitize_plan(raw_plan: dict[str, Any], source_model: str = "offline") -> SanitizedPlan:
    raw_warnings = raw_plan.get("warnings")
    warnings: list[str] = [str(warning)[:240] for warning in raw_warnings] if isinstance(raw_warnings, list) else []
    title = _clean_title(raw_plan.get("title"))
    description = str(raw_plan.get("description") or "").strip()[:240]
    raw_commands = raw_plan.get("commands")
    if not isinstance(raw_commands, list):
        raw_commands = []
        warnings.append("missing commands list; emitted an empty plan")

    commands: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_commands[:MAX_COMMANDS]):
        if not isinstance(raw, dict):
            warnings.append(f"dropped command {index}: not an object")
            continue
        command_type = str(raw.get("type") or "").lower()
        if command_type not in ALLOWED_COMMANDS:
            warnings.append(f"dropped command {index}: unsupported type {command_type!r}")
            continue

        if command_type == "line":
            commands.append(
                {
                    "type": "line",
                    "x1": round(_clamp(_number(raw.get("x1")), 0.0, PAGE_WIDTH_MM), 3),
                    "y1": round(_clamp(_number(raw.get("y1")), 0.0, PAGE_HEIGHT_MM), 3),
                    "x2": round(_clamp(_number(raw.get("x2")), 0.0, PAGE_WIDTH_MM), 3),
                    "y2": round(_clamp(_number(raw.get("y2")), 0.0, PAGE_HEIGHT_MM), 3),
                }
            )
        elif command_type == "polyline":
            raw_points = raw.get("points")
            if not isinstance(raw_points, list):
                warnings.append(f"dropped command {index}: polyline points must be a list")
                continue
            points = [_point(point, warnings, f"polyline {index}") for point in raw_points[:MAX_POINTS_PER_POLYLINE]]
            clean_points = [point for point in points if point is not None]
            if len(clean_points) < 2:
                warnings.append(f"dropped command {index}: polyline needs at least two valid points")
                continue
            commands.append({"type": "polyline", "points": clean_points})
        elif command_type == "circle":
            cx = _clamp(_number(raw.get("cx")), 0.0, PAGE_WIDTH_MM)
            cy = _clamp(_number(raw.get("cy")), 0.0, PAGE_HEIGHT_MM)
            max_radius = min(cx, cy, PAGE_WIDTH_MM - cx, PAGE_HEIGHT_MM - cy)
            radius = _clamp(_number(raw.get("r"), 1.0), 0.5, max(0.5, max_radius))
            commands.append({"type": "circle", "cx": round(cx, 3), "cy": round(cy, 3), "r": round(radius, 3)})
        elif command_type == "text":
            value = str(raw.get("value") or "")[:80]
            if not value:
                warnings.append(f"dropped command {index}: empty text")
                continue
            commands.append(
                {
                    "type": "text",
                    "x": round(_clamp(_number(raw.get("x")), 0.0, PAGE_WIDTH_MM), 3),
                    "y": round(_clamp(_number(raw.get("y")), 0.0, PAGE_HEIGHT_MM), 3),
                    "value": value,
                    "size": round(_clamp(_number(raw.get("size"), 6.0), 2.0, 16.0), 3),
                }
            )

    if len(raw_commands) > MAX_COMMANDS:
        warnings.append(f"truncated command list from {len(raw_commands)} to {MAX_COMMANDS}")

    return SanitizedPlan(
        title=title,
        description=description,
        commands=commands,
        warnings=warnings,
        source_model=source_model,
    )


def estimate_pen_distance_mm(commands: list[dict[str, Any]]) -> float:
    total = 0.0
    for command in commands:
        if command["type"] == "line":
            dx = command["x2"] - command["x1"]
            dy = command["y2"] - command["y1"]
            total += (dx * dx + dy * dy) ** 0.5
        elif command["type"] == "polyline":
            points = command["points"]
            for (x1, y1), (x2, y2) in zip(points, points[1:]):
                dx = x2 - x1
                dy = y2 - y1
                total += (dx * dx + dy * dy) ** 0.5
        elif command["type"] == "circle":
            total += 2 * 3.14159 * command["r"]
        elif command["type"] == "text":
            total += len(command["value"]) * command["size"] * 0.7
    return round(total, 1)


def text_command_to_paths(command: dict[str, Any]) -> list[str]:
    text = command["value"].upper()
    cell = command["size"] / 7.0
    spacing = cell
    char_width = 5 * cell + spacing
    origin_x = command["x"]
    origin_y = command["y"] - command["size"]
    paths: list[str] = []
    for char_index, char in enumerate(text):
        if char == " ":
            continue
        glyph = GLYPHS.get(char)
        if not glyph:
            continue
        char_x = origin_x + char_index * char_width
        for row, pattern in enumerate(glyph):
            for col, bit in enumerate(pattern):
                if bit != "1":
                    continue
                x = round(char_x + col * cell, 3)
                y = round(origin_y + row * cell, 3)
                s = round(cell * 0.72, 3)
                paths.append(f'M {x} {y} h {s} v {s} h {-s} Z')
    return paths


def plan_to_svg(plan: SanitizedPlan) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_WIDTH_MM}mm" '
            f'height="{PAGE_HEIGHT_MM}mm" viewBox="0 0 {PAGE_WIDTH_MM} {PAGE_HEIGHT_MM}">'
        ),
        f"  <title>{html.escape(plan.title)}</title>",
        f"  <desc>{html.escape(plan.description)}</desc>",
        '  <g fill="none" stroke="black" stroke-width="0.35" stroke-linecap="round" stroke-linejoin="round">',
    ]
    for command in plan.commands:
        command_type = command["type"]
        if command_type == "line":
            lines.append(
                f'    <line x1="{command["x1"]}" y1="{command["y1"]}" '
                f'x2="{command["x2"]}" y2="{command["y2"]}" />'
            )
        elif command_type == "polyline":
            points = " ".join(f"{x},{y}" for x, y in command["points"])
            lines.append(f'    <polyline points="{points}" />')
        elif command_type == "circle":
            lines.append(f'    <circle cx="{command["cx"]}" cy="{command["cy"]}" r="{command["r"]}" />')
        elif command_type == "text":
            lines.append(f'    <g aria-label="{html.escape(command["value"])}">')
            for path_data in text_command_to_paths(command):
                lines.append(f'      <path d="{path_data}" />')
            lines.append("    </g>")
    lines.extend(["  </g>", "</svg>", ""])
    return "\n".join(lines)


def write_outputs(plan: SanitizedPlan, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{plan.title}.plan.json"
    svg_path = output_dir / f"{plan.title}.svg"
    json_payload = {
        "title": plan.title,
        "description": plan.description,
        "source_model": plan.source_model,
        "page": {"width_mm": PAGE_WIDTH_MM, "height_mm": PAGE_HEIGHT_MM},
        "commands": plan.commands,
        "metrics": {
            "command_count": len(plan.commands),
            "estimated_pen_distance_mm": estimate_pen_distance_mm(plan.commands),
        },
        "warnings": plan.warnings,
    }
    json_path.write_text(json.dumps(json_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    svg_path.write_text(plan_to_svg(plan), encoding="utf-8")
    return json_path, svg_path
