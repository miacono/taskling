"""Reminder times of tasks, kept on this computer.

The Google Tasks API only stores the due date of a task, not a time, so Taskling keeps the time
locally and the tray app shows a desktop notification on the due date at that time.
"""

from datetime import date, datetime, time

from . import config

REMINDERS_FILE = config.CONFIG_DIR / "reminders.json"


def load() -> dict:
    """Task id -> {"tasklist": id, "time": "HH:MM", "notified": "YYYY-MM-DDTHH:MM" once shown}."""
    return config.read_json(REMINDERS_FILE) or {}


def save(saved: dict) -> None:
    config.write_private_json(REMINDERS_FILE, saved)


def time_of(task_id: str) -> str | None:
    entry = load().get(task_id)
    return entry["time"] if entry else None


def moment(due: date, at: str) -> datetime:
    return datetime.combine(due, time.fromisoformat(at))


def set_time(tasklist_id: str, task_id: str, at: str | None, due: date | None) -> None:
    """Set the reminder time of a task, or remove it with None. A moment already past is not notified."""
    saved = load()
    if at is None:
        if saved.pop(task_id, None) is None:
            return
    else:
        entry = {"tasklist": tasklist_id, "time": at}
        if due and moment(due, at) <= datetime.now():
            entry["notified"] = moment(due, at).isoformat(timespec="minutes")
        saved[task_id] = entry
    save(saved)
