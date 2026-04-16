"""Append-only event log for job-level audit trail.

Each event is written as a separate JSON file under ``job_dir/events/``.
File names are naturally sortable: ``<ISO8601>_<node_id>_<event_type>.json``.
No locking is required — each write creates a new file.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def write_event(
    job_dir: str,
    node_id: str,
    event_type: str,
    *,
    tool: Optional[str] = None,
    success: Optional[bool] = None,
    cli: Optional[str] = None,
    details: Optional[dict] = None,
) -> Path:
    """Write a single event file to ``job_dir/events/``.

    Returns the path of the created event file.
    """
    events_dir = Path(job_dir) / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"  # ms precision

    safe_node = re.sub(r"[^a-zA-Z0-9_-]", "_", node_id)
    safe_type = re.sub(r"[^a-zA-Z0-9_-]", "_", event_type)
    uid = uuid.uuid4().hex[:8]
    filename = f"{ts}_{safe_node}_{safe_type}_{uid}.json"

    event = {
        "timestamp": now.isoformat(),
        "node_id": node_id,
        "event_type": event_type,
    }
    if tool is not None:
        event["tool"] = tool
    if success is not None:
        event["success"] = success
    if cli is not None:
        event["cli"] = cli
    if details is not None:
        event["details"] = details

    event_path = events_dir / filename
    # Use atomic write: write to tmp then rename
    tmp_path = event_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(event, indent=2, default=str))
    os.replace(str(tmp_path), str(event_path))

    logger.debug(f"Event written: {event_path.name}")
    return event_path


def read_events(
    job_dir: str,
    *,
    node_id: Optional[str] = None,
    event_type: Optional[str] = None,
) -> list[dict]:
    """Read events from ``job_dir/events/``, optionally filtered.

    Returns a list of event dicts sorted by timestamp (ascending).
    """
    events_dir = Path(job_dir) / "events"
    if not events_dir.is_dir():
        return []

    events: list[dict] = []
    for p in sorted(events_dir.glob("*.json")):
        try:
            ev = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if node_id and ev.get("node_id") != node_id:
            continue
        if event_type and ev.get("event_type") != event_type:
            continue
        events.append(ev)
    return events
