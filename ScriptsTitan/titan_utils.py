import re


TIMESTAMP_PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*(.*)$")
REMOVED_LINE_PREFIXES = [
    "[DIAG ] ⚠ Poll limit hit for",
    "[DIAG ] ⚠ HTTP 422 from https://gamma-api.polymarket.com/markets",
]


def _line_dedup_key(line: str) -> str:
    match = TIMESTAMP_PREFIX_RE.match(line)
    return match.group(1) if match else line


def _strip_timestamp_prefix(line: str) -> str:
    match = TIMESTAMP_PREFIX_RE.match(line)
    return match.group(1) if match else line


def _compact_log_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if not lines:
        return text

    removed_counts: dict[str, int] = {prefix: 0 for prefix in REMOVED_LINE_PREFIXES}
    filtered_lines: list[str] = []
    for line in lines:
        content = _strip_timestamp_prefix(line)
        removed = False
        for prefix in REMOVED_LINE_PREFIXES:
            if content.startswith(prefix):
                removed_counts[prefix] += 1
                removed = True
                break
        if not removed:
            filtered_lines.append(line)

    summary_lines = [
        f"[removed] {count} line(s): {prefix}"
        for prefix, count in removed_counts.items()
        if count
    ]

    compacted: list[str] = []
    last_key: str | None = None
    duplicate_count = 0

    def flush_duplicates() -> None:
        nonlocal duplicate_count
        if duplicate_count:
            compacted.append(f"[dedup] removed {duplicate_count} duplicate line(s) matching the previous entry")
            duplicate_count = 0

    for line in filtered_lines:
        key = _line_dedup_key(line)
        if last_key is None:
            compacted.append(line)
            last_key = key
            continue

        if key == last_key:
            duplicate_count += 1
            continue

        flush_duplicates()
        compacted.append(line)
        last_key = key

    flush_duplicates()

    if len(summary_lines) >= max_lines:
        return "\n".join(summary_lines[:max_lines])

    body_budget = max_lines - len(summary_lines)
    if len(compacted) <= body_budget:
        return "\n".join(summary_lines + compacted)

    head_count = body_budget // 2
    tail_count = body_budget - head_count - 1
    omitted = len(compacted) - body_budget + 1
    trimmed = summary_lines + compacted[:head_count]
    trimmed.append(f"[trimmed] omitted {omitted} line(s) to cap log tail at {max_lines} lines")
    trimmed.extend(compacted[-tail_count:])
    return "\n".join(trimmed)
