"""Command line interface. Without a command, Taskling starts the tray app."""

import argparse
import json
import sys
from datetime import date, datetime, timedelta

from . import __version__, api, auth, config, reminders

DATE_WORDS = {"today": 0, "tomorrow": 1}


def parse_date(text: str) -> date | str:
    """YYYY-MM-DD, DD/MM[/YYYY], today or tomorrow; "" is kept to clear the due date."""
    if not text:
        return ""
    if text.casefold() in DATE_WORDS:
        return date.today() + timedelta(days=DATE_WORDS[text.casefold()])
    try:
        if "/" in text:
            day, month, *year = (int(part) for part in text.split("/"))
            year = year[0] if year else date.today().year
            return date(year + 2000 if year < 100 else year, month, day)
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date {text!r}: use YYYY-MM-DD, DD/MM[/YYYY], today or tomorrow") from None


def parse_time(text: str) -> str:
    """HH:MM or HH.MM, normalized to HH:MM; "" is kept to clear the reminder."""
    if not text:
        return ""
    try:
        return datetime.strptime(text.replace(".", ":"), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid time {text!r}: use HH:MM") from None


def _tasklist(args) -> api.TaskList:
    wanted = args.tasklist or config.load_settings().get("tasklist")
    tasklist = api.find_tasklist(api.tasklists(), wanted)
    if tasklist is None:
        sys.exit(f"taskling: no task list matches {wanted!r} (see 'taskling lists')")
    return tasklist


def _pick(tasks: list[api.Task], refs: list[str]) -> list[api.Task]:
    """Resolve task numbers as shown by 'taskling ls' (or task ids) before changing anything."""
    picked = []
    for ref in refs:
        if ref.isdigit() and 1 <= int(ref) <= len(tasks):
            picked.append(tasks[int(ref) - 1])
        elif task := next((task for task in tasks if task.id == ref), None):
            picked.append(task)
        else:
            sys.exit(f"taskling: no task {ref!r} (see 'taskling ls -a')")
    return picked


def cmd_auth(args) -> None:
    auth.authorize()
    print("Signed in.")


def cmd_lists(args) -> None:
    lists = api.tasklists()
    current = api.find_tasklist(lists, config.load_settings().get("tasklist"))
    for number, tasklist in enumerate(lists, 1):
        marker = "*" if current and tasklist.id == current.id else " "
        print(f"{marker} {number}. {tasklist.title}")


def cmd_use(args) -> None:
    tasklist = api.find_tasklist(api.tasklists(), args.name)
    if tasklist is None:
        sys.exit(f"taskling: no task list matches {args.name!r} (see 'taskling lists')")
    settings = config.load_settings()
    settings["tasklist"] = tasklist.id
    config.save_settings(settings)
    print(f"Using {tasklist.title}.")


def cmd_ls(args) -> None:
    tasklist = _tasklist(args)
    tasks = api.tasks(tasklist.id)
    shown = [(number, task) for number, task in enumerate(tasks, 1) if args.all or not task.done]
    saved = reminders.load()
    remind_at = {task.id: saved[task.id]["time"] for task in tasks if task.id in saved and task.due}
    if args.json:
        json.dump({
            "tasklist": {"id": tasklist.id, "title": tasklist.title},
            "tasks": [{
                "number": number, "id": task.id, "title": task.title, "notes": task.notes,
                "done": task.done, "parent": task.parent, "due": task.due[:10] or None,
                "remind_at": remind_at.get(task.id), "completed": task.completed,
            } for number, task in shown],
        }, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return
    print(tasklist.title)
    if not shown:
        print("  nothing to do")
    open_ids = {task.id for task in tasks if not task.done}
    for number, task in shown:
        indent = "    " if task.parent in open_ids else ""
        at = f" {remind_at[task.id]}" if task.id in remind_at else ""
        due = f"  (due {task.due[:10]}{at})" if task.due else ""
        print(f"{number:>3}. [{'x' if task.done else ' '}] {indent}{task.title}{due}")
        if args.details:
            for line in task.notes.splitlines():
                print(f"         {indent}{line}")


def cmd_add(args) -> None:
    due = args.due or (date.today() if args.at else None)  # a reminder needs a due date
    tasklist = _tasklist(args)
    task = api.add_task(tasklist.id, args.title, args.notes or "", due)
    if args.at:
        reminders.set_time(tasklist.id, task.id, args.at, due)
    print(f"Added: {task.title}")


def _change(args, action: str) -> None:
    tasklist = _tasklist(args)
    for task in _pick(api.tasks(tasklist.id), args.tasks):
        if action == "rm":
            api.delete(tasklist.id, task.id)
            reminders.set_time(tasklist.id, task.id, None, None)
            print(f"Deleted: {task.title}")
        else:
            api.set_done(tasklist.id, task.id, action == "done")
            print(f"{'Done' if action == 'done' else 'Reopened'}: {task.title}")


def cmd_mv(args) -> None:
    tasklist = _tasklist(args)
    tasks = api.tasks(tasklist.id)
    [task] = _pick(tasks, [args.task])
    if args.after:
        [anchor] = _pick(tasks, [args.after])
        parent, previous = anchor.parent, anchor.id
    elif args.under:
        [anchor] = _pick(tasks, [args.under])
        parent, previous = anchor.id, None
    else:
        parent, previous = None, None
    if task.id in (previous, parent):
        sys.exit("taskling: a task cannot be moved relative to itself")
    api.move(tasklist.id, task.id, parent, previous)
    print(f"Moved: {task.title}")


def cmd_edit(args) -> None:
    if all(value is None for value in (args.title, args.notes, args.due, args.at)):
        sys.exit("taskling: nothing to change, give a new title, -n DETAILS, --due DATE and/or --at TIME")
    if args.due == "" and args.at:
        sys.exit("taskling: a reminder needs a due date")
    tasklist = _tasklist(args)
    [task] = _pick(api.tasks(tasklist.id), [args.task])
    due = args.due
    if args.at and due is None and not task.due:
        due = date.today()  # a reminder needs a due date
    if (args.title, args.notes, due) != (None, None, None):
        task = api.update(tasklist.id, task.id, args.title, args.notes, due)
    if due == "":
        reminders.set_time(tasklist.id, task.id, None, None)
    elif args.at is not None or (due and reminders.time_of(task.id)):
        at = reminders.time_of(task.id) if args.at is None else args.at
        reminders.set_time(tasklist.id, task.id, at or None, task.due_date)
    print(f"Updated: {task.title}")


def main(argv: list[str] | None = None) -> int | None:
    parser = argparse.ArgumentParser(prog="taskling", description="Google Tasks from the Linux tray and terminal.")
    parser.add_argument("--version", action="version", version=f"taskling {__version__}")
    list_option = argparse.ArgumentParser(add_help=False)
    list_option.add_argument("-l", "--list", dest="tasklist",
                             help="task list title, number or id (default: the one chosen with 'use')")
    commands = parser.add_subparsers(dest="command", metavar="command")

    commands.add_parser("tray", help="run the system tray app (the default)")
    commands.add_parser("auth", help="sign in to Google")
    commands.add_parser("lists", help="show the task lists, * marks the current one")
    use = commands.add_parser("use", help="choose the current task list")
    use.add_argument("name", help="list title, number or id")
    ls = commands.add_parser("ls", parents=[list_option], help="show open tasks, numbered")
    ls.add_argument("-a", "--all", action="store_true", help="include completed tasks")
    ls.add_argument("-d", "--details", action="store_true", help="show the details under each task")
    ls.add_argument("--json", action="store_true", help="print JSON for scripts")
    add = commands.add_parser("add", parents=[list_option], help="add a task")
    add.add_argument("title")
    add.add_argument("-n", "--notes", metavar="DETAILS", help="task details")
    add.add_argument("--due", type=parse_date, metavar="DATE",
                     help="due date: YYYY-MM-DD, DD/MM[/YYYY], today or tomorrow")
    add.add_argument("--at", type=parse_time, metavar="HH:MM",
                     help="reminder time on the due date (default date: today), notified by the tray app")
    for name, help_text in (("done", "mark tasks as done"), ("undo", "reopen completed tasks"), ("rm", "delete tasks")):
        sub = commands.add_parser(name, parents=[list_option], help=help_text)
        sub.add_argument("tasks", nargs="+", help="task numbers from 'ls' (or ids)")
    mv = commands.add_parser("mv", parents=[list_option], help="move a task in the list")
    mv.add_argument("task", help="task number from 'ls' (or id)")
    where = mv.add_mutually_exclusive_group(required=True)
    where.add_argument("--top", action="store_true", help="move it to the top of the list")
    where.add_argument("--after", metavar="TASK", help="move it right after this task, at the same level")
    where.add_argument("--under", metavar="TASK", help="make it the first subtask of this task")
    edit = commands.add_parser("edit", parents=[list_option],
                               help="change the title, details, due date or reminder of a task")
    edit.add_argument("task", help="task number from 'ls' (or id)")
    edit.add_argument("title", nargs="?", help="new title")
    edit.add_argument("-n", "--notes", metavar="DETAILS", help='new details, "" clears them')
    edit.add_argument("--due", type=parse_date, metavar="DATE", help='new due date, "" clears it and the reminder')
    edit.add_argument("--at", type=parse_time, metavar="HH:MM", help='new reminder time, "" clears it')

    args = parser.parse_args(argv)
    if args.command in (None, "tray"):
        from .tray import run  # Qt is only needed by the tray app

        return run()

    handlers = {
        "auth": cmd_auth, "lists": cmd_lists, "use": cmd_use, "ls": cmd_ls, "add": cmd_add, "edit": cmd_edit,
        "mv": cmd_mv,
        "done": lambda args: _change(args, "done"),
        "undo": lambda args: _change(args, "undo"),
        "rm": lambda args: _change(args, "rm"),
    }
    try:
        handlers[args.command](args)
    except (api.ApiError, auth.AuthError) as e:
        sys.exit(f"taskling: {e}")
    return None
