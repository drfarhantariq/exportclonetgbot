from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .msz_api import norm_rel


INDEX_ROW_RE = re.compile(r"^\[(?P<enabled>[xX ]?)]\s+(?P<message_id>\d+)\s*\|\s*(?P<level>P|S\d*)\s*\|\s*(?P<name>.+?)\s*$", re.IGNORECASE)
TOPIC_LINK_RE = re.compile(r"^#\s*topic_link:\s*(?P<link>\S+)\s*$")
TOPIC_TITLE_RE = re.compile(r"^#\s*topic_title:\s*(?P<title>.*?)\s*$")
START_MESSAGE_RE = re.compile(r"^#\s*start_message_id:\s*(?P<message_id>\d+)\s*$")
UNSORTED_FOLDER = "_Unsorted"


@dataclass(frozen=True)
class FolderHeading:
    message_id: int
    level: int
    name: str
    enabled: bool = True


@dataclass(frozen=True)
class FolderIndex:
    topic_link: str
    headings: list[FolderHeading]
    start_message_id: int = 0
    topic_title: str = ""


def normalize_heading_text(text: str) -> str:
    return " ".join(part.strip() for part in str(text).splitlines() if part.strip())


def validate_path_segment(value: str) -> str:
    segment = Path(value.replace("\x00", "")).name.strip()
    if not segment:
        raise ValueError("Folder name cannot be empty.")
    if segment in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"Unsafe folder name: {value!r}")
    return segment


def level_to_label(level: int) -> str:
    if level < 1:
        raise ValueError(f"Invalid heading level: {level}")
    if level == 1:
        return "P"
    if level == 2:
        return "S"
    return f"S{level - 2}"


def label_to_level(label: str) -> int:
    value = label.strip().upper()
    if value == "P":
        return 1
    if value == "S":
        return 2
    if value.startswith("S") and value[1:].isdigit():
        sub_depth = int(value[1:])
        if sub_depth < 1:
            raise ValueError(f"Invalid heading level label: {label!r}")
        return sub_depth + 2
    raise ValueError(f"Invalid heading level label: {label!r}. Use P, S, S1, S2, etc.")


def format_index(
    topic_link: str,
    headings: Iterable[FolderHeading],
    start_message_id: int = 0,
    topic_title: str = "",
) -> str:
    lines = [
        "# Telegram folder index",
        f"# topic_link: {topic_link}",
        f"# topic_title: {normalize_heading_text(topic_title)}" if topic_title else "# topic_title:",
        f"# start_message_id: {start_message_id}" if start_message_id else "# start_message_id: 0",
        "# Syntax:",
        "# [x] <message_id> | <P/S/S1/S2> | <folder name>",
        "# P = parent, S = subfolder, S1/S2/etc = deeper subfolders",
        "# Disable a folder heading with [ ]",
        "",
    ]
    for heading in headings:
        marker = "x" if heading.enabled else " "
        lines.append(
            f"[{marker}] {heading.message_id} | {level_to_label(heading.level)} | {normalize_heading_text(heading.name)}"
        )
    return "\n".join(lines).rstrip() + "\n"


def parse_index(path: Path) -> FolderIndex:
    topic_link = ""
    topic_title = ""
    start_message_id = 0
    headings: list[FolderHeading] = []
    seen_ids: set[int] = set()

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        topic_match = TOPIC_LINK_RE.match(line)
        if topic_match:
            topic_link = topic_match.group("link").strip()
            continue
        title_match = TOPIC_TITLE_RE.match(line)
        if title_match:
            topic_title = normalize_heading_text(title_match.group("title"))
            if topic_title:
                validate_path_segment(topic_title)
            continue
        start_match = START_MESSAGE_RE.match(line)
        if start_match:
            start_message_id = int(start_match.group("message_id"))
            continue
        if line.startswith("#"):
            continue

        match = INDEX_ROW_RE.match(line)
        if not match:
            raise ValueError(f"Invalid index row at line {line_number}: {raw_line}")

        message_id = int(match.group("message_id"))
        if message_id in seen_ids:
            raise ValueError(f"Duplicate heading message id at line {line_number}: {message_id}")
        seen_ids.add(message_id)

        try:
            level = label_to_level(match.group("level"))
        except ValueError as exc:
            raise ValueError(f"Invalid heading level at line {line_number}: {match.group('level')}") from exc
        name = validate_path_segment(match.group("name"))
        headings.append(
            FolderHeading(
                message_id=message_id,
                level=level,
                name=name,
                enabled=match.group("enabled").lower() == "x",
            )
        )

    if not topic_link:
        raise ValueError("Index is missing '# topic_link: ...' header.")
    validate_heading_hierarchy(headings)
    return FolderIndex(
        topic_link=topic_link,
        headings=headings,
        start_message_id=start_message_id,
        topic_title=topic_title,
    )


def validate_heading_hierarchy(headings: Iterable[FolderHeading]) -> None:
    stack: list[str] = []
    for heading in headings:
        if not heading.enabled:
            continue
        validate_path_segment(heading.name)
        if heading.level > len(stack) + 1:
            raise ValueError(
                f"Heading {heading.message_id} jumps to level {heading.level} without a level {len(stack) + 1} parent."
            )
        stack = stack[: heading.level - 1]
        stack.append(heading.name)


def folder_paths_by_heading(headings: Iterable[FolderHeading]) -> dict[int, str]:
    paths: dict[int, str] = {}
    stack: list[str] = []
    for heading in headings:
        if not heading.enabled:
            continue
        if heading.level > len(stack) + 1:
            raise ValueError(
                f"Heading {heading.message_id} jumps to level {heading.level} without a level {len(stack) + 1} parent."
            )
        stack = stack[: heading.level - 1]
        stack.append(validate_path_segment(heading.name))
        paths[heading.message_id] = norm_rel("/".join(stack))
    return paths


def assign_media_folder(
    message_id: int,
    active_folder: str,
) -> str:
    return norm_rel(active_folder or UNSORTED_FOLDER)
