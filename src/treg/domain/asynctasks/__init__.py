"""Pure runtime semantics and state transitions for deferred async settlement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit


PENDING = "pending"
SETTLED = "settled"
RELEASED = "released"
TIMED_OUT = "timed_out"
TERMINAL_STATUSES = frozenset({SETTLED, RELEASED, TIMED_OUT})
MAX_AGE = timedelta(hours=24)


class ExtractionError(ValueError):
    """A provider submission no longer matches its frozen async descriptor."""


@dataclass(frozen=True)
class Submission:
    task_id: str
    poll_url: str | None


def json_path(document: object, dotted: str) -> object:
    current = document
    for part in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def extract_submission(descriptor: dict, response: object) -> Submission:
    task_id = json_path(response, str(descriptor.get("id_from") or ""))
    if task_id in (None, ""):
        raise ExtractionError("submission response does not contain the async task id")
    poll = descriptor.get("poll") or {}
    poll_url = None
    if poll.get("url_from"):
        value = json_path(response, str(poll["url_from"]))
        if not isinstance(value, str) or not value:
            raise ExtractionError("submission response does not contain the async poll URL")
        parsed = urlsplit(value)
        hosts = {str(host).lower() for host in poll.get("url_hosts") or []}
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in hosts:
            raise ExtractionError("submission poll URL is not on the descriptor allow-list")
        poll_url = value
    return Submission(task_id=str(task_id), poll_url=poll_url)


def classify_terminal(descriptor: dict, response: object) -> str:
    status_rule = descriptor.get("status") or {}
    value = json_path(response, str(status_rule.get("path") or ""))
    if value is None:
        return "progress"
    status = str(value)
    if status in {str(item) for item in status_rule.get("success", [])}:
        return "success"
    if status in {str(item) for item in status_rule.get("failure", [])}:
        return "failure"
    return "progress"


def next_check(now: datetime, attempts: int) -> datetime:
    """First retry is 30 seconds; later retries grow to the frozen 60-second ceiling."""
    return now + timedelta(seconds=min(60, 30 + max(0, attempts) * 10))


def expired(created_at: datetime, now: datetime) -> bool:
    return now - created_at >= MAX_AGE
