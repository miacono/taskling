"""Minimal client for the Google Tasks REST API."""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date

from . import auth

BASE_URL = "https://tasks.googleapis.com/tasks/v1"


class ApiError(Exception):
    pass


@dataclass
class TaskList:
    id: str
    title: str


@dataclass
class Task:
    id: str
    title: str
    notes: str
    done: bool
    parent: str | None
    position: str
    completed: str  # RFC 3339 completion time, empty while open
    due: str  # RFC 3339 timestamp whose time is always midnight UTC, empty when unset

    @property
    def due_date(self) -> date | None:
        return date.fromisoformat(self.due[:10]) if self.due else None

    @classmethod
    def from_json(cls, data: dict) -> "Task":
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            notes=data.get("notes", ""),
            done=data.get("status") == "completed",
            parent=data.get("parent"),
            position=data.get("position", ""),
            completed=data.get("completed", ""),
            due=data.get("due", ""),
        )


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None,
             retry: bool = True) -> dict | None:
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    # A 401 on the first attempt means the token was revoked or expired early: refresh and retry once.
    request.add_header("Authorization", f"Bearer {auth.access_token(force_refresh=not retry)}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            return _request(method, path, params, body, retry=False)
        try:
            message = json.load(e)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = str(e)
        raise ApiError(message) from None
    except urllib.error.URLError as e:
        raise ApiError(f"network error: {e.reason}") from None
    return json.loads(raw) if raw else None


def _paginate(path: str, params: dict):
    params = dict(params, maxResults=100)
    while True:
        page = _request("GET", path, params)
        yield from page.get("items", [])
        if not page.get("nextPageToken"):
            return
        params["pageToken"] = page["nextPageToken"]


def _task_path(tasklist_id: str, task_id: str = "") -> str:
    path = f"/lists/{urllib.parse.quote(tasklist_id, safe='')}/tasks"
    return path + f"/{urllib.parse.quote(task_id, safe='')}" if task_id else path


def tasklists() -> list[TaskList]:
    """All task lists; Google returns the default list first."""
    return [TaskList(item["id"], item["title"]) for item in _paginate("/users/@me/lists", {})]


def find_tasklist(lists: list[TaskList], wanted: str | None) -> TaskList | None:
    """Match a list by id, title (case-insensitive) or 1-based number; None picks the default."""
    if not wanted:
        return lists[0] if lists else None
    if wanted.isdigit() and 1 <= int(wanted) <= len(lists):
        return lists[int(wanted) - 1]
    for tasklist in lists:
        if wanted == tasklist.id or wanted.casefold() == tasklist.title.casefold():
            return tasklist
    return None


def tasks(tasklist_id: str) -> list[Task]:
    """All tasks of a list in display order, see ordered()."""
    items = _paginate(_task_path(tasklist_id), {"showCompleted": "true", "showHidden": "true"})
    return ordered([Task.from_json(item) for item in items])


def ordered(items: list[Task]) -> list[Task]:
    """Open tasks in Google Tasks order with subtasks under their parent, then completed ones, newest first."""
    open_tasks = [task for task in items if not task.done]
    open_ids = {task.id for task in open_tasks}
    children: dict[str | None, list[Task]] = {}
    for task in open_tasks:
        children.setdefault(task.parent if task.parent in open_ids else None, []).append(task)
    for siblings in children.values():
        siblings.sort(key=lambda task: task.position)

    result: list[Task] = []

    def visit(parent_id: str | None) -> None:
        for task in children.get(parent_id, []):
            result.append(task)
            visit(task.id)

    visit(None)
    completed = sorted((task for task in items if task.done), key=lambda task: task.completed, reverse=True)
    return result + completed


def due_value(due: date) -> str:
    """The API keeps only the date of a due timestamp and discards the time."""
    return f"{due.isoformat()}T00:00:00.000Z"


def add_task(tasklist_id: str, title: str, notes: str = "", due: date | None = None) -> Task:
    body = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        body["due"] = due_value(due)
    return Task.from_json(_request("POST", _task_path(tasklist_id), body=body))


def set_done(tasklist_id: str, task_id: str, done: bool) -> Task:
    body = {"status": "completed"} if done else {"status": "needsAction", "completed": None}
    return Task.from_json(_request("PATCH", _task_path(tasklist_id, task_id), body=body))


def update(tasklist_id: str, task_id: str, title: str | None = None, notes: str | None = None,
           due: date | str | None = None) -> Task:
    """Change title, details and/or due date; None leaves a field as is, "" clears details or due date."""
    body = {}
    if title is not None:
        body["title"] = title
    if notes is not None:
        body["notes"] = notes or None
    if due is not None:
        body["due"] = due_value(due) if due else None
    return Task.from_json(_request("PATCH", _task_path(tasklist_id, task_id), body=body))


def move(tasklist_id: str, task_id: str, parent: str | None = None, previous: str | None = None) -> Task:
    """Move a task after `previous` among the children of `parent`; None puts it first, or at the top level."""
    params = {key: value for key, value in (("parent", parent), ("previous", previous)) if value}
    # An empty JSON body, because Google rejects a POST without a Content-Length.
    return Task.from_json(_request("POST", _task_path(tasklist_id, task_id) + "/move", params, body={}))


def delete(tasklist_id: str, task_id: str) -> None:
    _request("DELETE", _task_path(tasklist_id, task_id))
