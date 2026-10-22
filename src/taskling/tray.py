"""System tray app: a checklist popup for one Google Tasks list, plus reminder notifications."""

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt6.QtCore import QDate, QLocale, QObject, QPoint, QRect, QSize, Qt, QTime, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QCursor, QGuiApplication, QIcon, QKeySequence, QPainter, QPalette, QPen,
                         QPixmap, QPolygon, QShortcut)
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QMenu, QPlainTextEdit, QStyledItemDelegate,
                             QSystemTrayIcon, QTimeEdit, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                             QWidget)

from . import api, auth, config, reminders

REFRESH_INTERVAL_MS = 5 * 60 * 1000
REMINDER_CHECK_MS = 30 * 1000
SCHEDULE_SAVE_DELAY_MS = 800  # lets the user finish typing a date or time before saving it
ID_ROLE = Qt.ItemDataRole.UserRole
TITLE, DUE = 0, 1  # tree columns
LATE_COLOR = QColor("#d93025")
TODAY_COLOR = QColor("#1a73e8")
LOG_FILE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "taskling" / "taskling.log"

log = logging.getLogger("taskling")


def make_icon() -> QIcon:
    """Draw the tray icon: a white tick on a rounded blue square."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(Qt.GlobalColor.transparent))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#1a73e8"))
    painter.drawRoundedRect(4, 4, 56, 56, 12, 12)
    painter.setPen(QPen(QColor("white"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin))
    painter.drawPolyline(QPolygon([QPoint(17, 33), QPoint(28, 44), QPoint(47, 22)]))
    painter.end()
    return QIcon(pixmap)


def make_details_icon() -> QIcon:
    """Draw the mark of tasks that have details: three lines of text."""
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor(Qt.GlobalColor.transparent))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#80868b"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    for y, width in ((4, 12), (8, 12), (12, 7)):
        painter.drawLine(2, y, 2 + width, y)
    painter.end()
    return QIcon(pixmap)


def to_qdate(day: date) -> QDate:
    return QDate(day.year, day.month, day.day)


class DetailsEdit(QPlainTextEdit):
    focus_lost = pyqtSignal()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.focus_lost.emit()


class ReadOnlyDelegate(QStyledItemDelegate):
    """Keeps a column out of in-place editing."""

    def createEditor(self, parent, option, index):
        return None


class Worker(QObject):
    """Runs API calls one at a time off the GUI thread and delivers the results back on it."""

    _finished = pyqtSignal(object, object, object)

    def __init__(self):
        super().__init__()
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._finished.connect(lambda on_done, result, error: on_done(result, error))

    def run(self, fn, *args, on_done) -> None:
        def job():
            try:
                result, error = fn(*args), None
            except Exception as e:  # reported to on_done, never lost in the thread
                result, error = None, e
            self._finished.emit(on_done, result, error)

        self._pool.submit(job)


class TaskWindow(QWidget):
    open_count_changed = pyqtSignal(int)
    tasks_loaded = pyqtSignal(object)

    def __init__(self, worker: Worker):
        super().__init__(None, Qt.WindowType.Popup)
        self.worker = worker
        self.tasklists: list[api.TaskList] = []
        self.current: api.TaskList | None = None
        self.tasks: list[api.Task] = []
        self.hidden_at = 0.0
        self.details_task_id: str | None = None
        self.details_icon = make_details_icon()
        self.loading_schedule = False
        self.pending_changes = 0
        self.resize(380, 560)

        self.list_combo = QComboBox()
        self.list_combo.currentIndexChanged.connect(self._on_list_changed)
        self.show_done = QToolButton()
        self.show_done.setText("✓")
        self.show_done.setCheckable(True)
        self.show_done.setToolTip("Show completed tasks")
        self.show_done.toggled.connect(self._populate)
        refresh = QToolButton()
        refresh.setIcon(QIcon.fromTheme("view-refresh"))
        if refresh.icon().isNull():
            refresh.setText("⟳")
        refresh.setToolTip("Refresh")
        refresh.clicked.connect(self.refresh)
        header = QHBoxLayout()
        header.addWidget(self.list_combo, 1)
        header.addWidget(self.show_done)
        header.addWidget(refresh)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(TITLE, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(DUE, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setItemDelegateForColumn(DUE, ReadOnlyDelegate(self.tree))
        self.tree.setRootIsDecorated(False)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_item_menu)
        delete = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.tree)
        delete.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete.activated.connect(self._delete_current)

        # Details of the selected task, saved when the editor loses focus.
        self.details = DetailsEdit()
        self.details.setFixedHeight(90)
        self.details.focus_lost.connect(self._save_details)

        # Due date (synced with Google) and reminder time (kept on this computer) of the selected task.
        self.due_check = QCheckBox("Due")
        self.due_check.setToolTip("Due date, synced with Google Tasks")
        self.due_edit = QDateEdit()
        self.due_edit.setCalendarPopup(True)
        self.due_edit.setDisplayFormat("ddd d MMM yyyy")
        self.time_check = QCheckBox("at")
        self.time_check.setToolTip("Desktop notification at this time on the due date.\n"
                                   "Google Tasks cannot store the time, so it stays on this computer.")
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setSingleShot(True)
        self.schedule_timer.setInterval(SCHEDULE_SAVE_DELAY_MS)
        self.schedule_timer.timeout.connect(self._save_schedule)
        for signal in (self.due_check.toggled, self.time_check.toggled,
                       self.due_edit.dateChanged, self.time_edit.timeChanged):
            signal.connect(self._on_schedule_edited)
        schedule = QHBoxLayout()
        schedule.addWidget(self.due_check)
        schedule.addWidget(self.due_edit, 1)
        schedule.addWidget(self.time_check)
        schedule.addWidget(self.time_edit)

        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Add a task…")
        self.entry.returnPressed.connect(self._add)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.hide()

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.tree)
        layout.addWidget(self.details)
        layout.addLayout(schedule)
        layout.addWidget(self.entry)
        layout.addWidget(self.status)
        self._show_details(None)

    # Showing and placing the popup

    def toggle_at(self, anchor: QRect) -> None:
        if self.isVisible():
            self.hide()
            return
        if time.monotonic() - self.hidden_at < 0.3:
            return  # this click on the tray icon is the one that just closed the popup
        self._place(anchor)
        self.show()
        self.activateWindow()
        self.entry.setFocus()
        self.refresh()

    def _place(self, anchor: QRect) -> None:
        if anchor.isEmpty():  # some trays do not report the icon geometry
            anchor = QRect(QCursor.pos(), QSize(1, 1))
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = anchor.center().x() - self.width() // 2
        if anchor.center().y() > area.center().y():  # panel at the bottom: open upwards
            y = anchor.top() - self.height() - 4
        else:
            y = anchor.bottom() + 4
        x = max(area.left(), min(x, area.right() - self.width()))
        y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)

    def hideEvent(self, event) -> None:
        log.debug("popup hidden")
        self._save_pending()
        self.hidden_at = time.monotonic()
        super().hideEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    # Loading

    def refresh(self) -> None:
        self.worker.run(self._load, on_done=self._loaded)

    @staticmethod
    def _load():  # runs on the worker thread
        lists = api.tasklists()
        current = api.find_tasklist(lists, config.load_settings().get("tasklist"))
        return lists, current, api.tasks(current.id) if current else []

    def _loaded(self, result, error) -> None:
        if error:
            self._show_error(error)
            return
        if self.pending_changes:
            log.debug("refresh ignored: %d change(s) still on their way", self.pending_changes)
            return  # loaded before a change still on its way: the refresh after that change shows it
        log.debug("refresh applied")
        self.tasklists, self.current, self.tasks = result
        self.list_combo.blockSignals(True)
        self.list_combo.clear()
        self.list_combo.addItems([tasklist.title for tasklist in self.tasklists])
        if self.current:
            self.list_combo.setCurrentIndex(self.tasklists.index(self.current))
        self.list_combo.blockSignals(False)
        self._populate()
        self._show_error(None)
        self.open_count_changed.emit(sum(not task.done for task in self.tasks))
        self.tasks_loaded.emit(self.tasks)

    def _populate(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        items: dict[str, QTreeWidgetItem] = {}
        muted = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        saved = reminders.load()
        now = datetime.now()
        for task in self.tasks:
            if task.done and not self.show_done.isChecked():
                continue
            item = QTreeWidgetItem([task.title])
            item.setData(TITLE, ID_ROLE, task.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable)
            item.setCheckState(TITLE, Qt.CheckState.Checked if task.done else Qt.CheckState.Unchecked)
            if task.notes:
                item.setToolTip(TITLE, task.notes)
                item.setIcon(TITLE, self.details_icon)
            if due := task.due_date:
                at = saved.get(task.id, {}).get("time")
                item.setText(DUE, QLocale().toString(to_qdate(due), "d MMM") + (f" {at}" if at else ""))
                late = reminders.moment(due, at) <= now if at else due < now.date()
                if task.done:
                    item.setForeground(DUE, muted)
                elif late:
                    item.setForeground(DUE, LATE_COLOR)
                elif due == now.date():
                    item.setForeground(DUE, TODAY_COLOR)
            if task.done:
                font = item.font(TITLE)
                font.setStrikeOut(True)
                item.setFont(TITLE, font)
                item.setForeground(TITLE, muted)
            parent = items.get(task.parent) if not task.done else None
            if parent:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            items[task.id] = item
        self.tree.expandAll()
        # Keep the selection across refreshes, without overwriting what is being typed.
        current = items.get(self.details_task_id)
        if current:
            self.tree.setCurrentItem(current)
        self.tree.blockSignals(False)
        if current is None:
            self._show_details(None)
        elif not self.details.hasFocus() and not self.schedule_timer.isActive():
            self._show_details(self._task(current))

    def _show_error(self, error: Exception | None) -> None:
        if error is None:
            self.status.hide()
        elif isinstance(error, auth.AuthError):
            self.status.setText(f"{error}\nRight-click the tray icon and choose “Sign in to Google…”.")
            self.status.show()
        else:
            self.status.setText(str(error))
            self.status.show()

    # Changes

    def _mutate(self, fn, *args) -> None:
        log.info("%s%s", fn.__name__, args[1:])
        self.pending_changes += 1
        self.worker.run(fn, *args, on_done=self._mutated)

    def _mutated(self, result, error) -> None:
        self.pending_changes -= 1
        if error:
            log.error("change failed: %s", error)
            self._show_error(error)
        self.refresh()

    def _find(self, task_id: str | None) -> api.Task | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def _task(self, item: QTreeWidgetItem) -> api.Task:
        return self._find(item.data(TITLE, ID_ROLE))

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != TITLE:
            return
        task = self._task(item)
        done = item.checkState(TITLE) == Qt.CheckState.Checked
        title = item.text(TITLE).strip()
        if done != task.done:
            self._mutate(api.set_done, self.current.id, task.id, done)
        elif title and title != task.title:
            self._mutate(api.update, self.current.id, task.id, title)
        elif not title:
            QTimer.singleShot(0, self._populate)  # restore the old title outside this signal

    def _on_current_changed(self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None) -> None:
        self._save_pending()
        self._show_details(self._task(current) if current else None)

    def _show_details(self, task: api.Task | None) -> None:
        if self.schedule_timer.isActive():
            log.warning("unsaved schedule discarded")
        log.debug("show %r: due=%s", task.title if task else None, task.due_date if task else None)
        self.schedule_timer.stop()
        self.details_task_id = task.id if task else None
        self.details.setEnabled(task is not None)
        self.details.setPlaceholderText("Add details…" if task else "Select a task to see its details")
        self.details.setPlainText(task.notes if task else "")
        due = task.due_date if task else None
        at = reminders.time_of(task.id) if task and due else None
        self.loading_schedule = True
        self.due_check.setChecked(due is not None)
        self.due_edit.setDate(to_qdate(due) if due else QDate.currentDate())
        self.time_check.setChecked(at is not None)
        self.time_edit.setTime(QTime.fromString(at, "HH:mm") if at else QTime(9, 0))
        self.loading_schedule = False
        self._enable_schedule()

    def _enable_schedule(self) -> None:
        selected = self.details_task_id is not None
        self.due_check.setEnabled(selected)
        self.time_check.setEnabled(selected)
        self.due_edit.setEnabled(selected and self.due_check.isChecked())
        self.time_edit.setEnabled(selected and self.time_check.isChecked())

    def _on_schedule_edited(self) -> None:
        if self.loading_schedule:
            return
        log.debug("schedule edited by %s: due=%s %s at=%s %s", type(self.sender()).__name__,
                  self.due_check.isChecked(), self.due_edit.date().toString(Qt.DateFormat.ISODate),
                  self.time_check.isChecked(), self.time_edit.time().toString("HH:mm"))
        # A reminder needs a due date: ticking the time ticks the date, unticking the date unticks the time.
        if self.sender() is self.time_check and self.time_check.isChecked():
            self.due_check.setChecked(True)
        elif self.sender() is self.due_check and not self.due_check.isChecked():
            self.time_check.setChecked(False)
        self._enable_schedule()
        self.schedule_timer.start()

    def _save_pending(self) -> None:
        self._save_details()
        if self.schedule_timer.isActive():
            self._save_schedule()

    def _save_details(self) -> None:
        task = self._find(self.details_task_id)
        notes = self.details.toPlainText().strip()
        if task and self.current and notes != task.notes.strip():
            task.notes = notes  # so the redraw before the refresh lands shows the new details
            self._mutate(api.update, self.current.id, task.id, None, notes)

    def _save_schedule(self) -> None:
        self.schedule_timer.stop()
        task = self._find(self.details_task_id)
        if task is None or self.current is None:
            return
        due = self.due_edit.date().toPyDate() if self.due_check.isChecked() else None
        at = self.time_edit.time().toString("HH:mm") if self.time_check.isChecked() else None
        log.debug("save schedule of %r: due %s -> %s, at %s -> %s",
                  task.title, task.due_date, due, reminders.time_of(task.id), at)
        if at != reminders.time_of(task.id) or (at and due != task.due_date):
            reminders.set_time(self.current.id, task.id, at, due)
        if due != task.due_date:
            task.due = api.due_value(due) if due else ""
            self._mutate(api.update, self.current.id, task.id, None, None, due or "")
        QTimer.singleShot(0, self._populate)  # show the new date and time in the list

    def _on_list_changed(self, index: int) -> None:
        if not 0 <= index < len(self.tasklists):
            return
        settings = config.load_settings()
        settings["tasklist"] = self.tasklists[index].id
        config.save_settings(settings)
        self.refresh()

    def _add(self) -> None:
        title = self.entry.text().strip()
        if title and self.current:
            self.entry.clear()
            self._mutate(api.add_task, self.current.id, title)

    def _delete_current(self) -> None:
        item = self.tree.currentItem()
        if item and self.current:
            task = self._task(item)
            reminders.set_time(self.current.id, task.id, None, None)
            self._mutate(api.delete, self.current.id, task.id)

    def _show_item_menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("Rename", lambda: self.tree.editItem(item, TITLE))
        menu.addAction("Delete", self._delete_current)
        menu.exec(self.tree.viewport().mapToGlobal(pos))


class ReminderNotifier(QObject):
    """Shows a desktop notification when the reminder time of an open task comes."""

    def __init__(self, worker: Worker, tray: QSystemTrayIcon):
        super().__init__()
        self.worker = worker
        self.tray = tray
        self.tasks: dict[str, api.Task] = {}  # latest known state of every task, by id
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check)
        self.timer.start(REMINDER_CHECK_MS)

    def remember(self, tasks: list[api.Task]) -> None:
        self.tasks.update({task.id: task for task in tasks})
        self.check()

    def reload(self) -> None:
        """Fetch every list that has reminders, so they fire even when another list is shown."""
        tasklist_ids = {entry["tasklist"] for entry in reminders.load().values()}
        if tasklist_ids:
            self.worker.run(self._fetch, tasklist_ids, on_done=self._fetched)

    @staticmethod
    def _fetch(tasklist_ids: set[str]) -> dict[str, list[api.Task]]:  # runs on the worker thread
        fetched = {}
        for tasklist_id in tasklist_ids:
            try:
                fetched[tasklist_id] = api.tasks(tasklist_id)
            except api.ApiError:
                pass  # tried again at the next reload
        return fetched

    def _fetched(self, fetched, error) -> None:
        if error:
            return
        saved = reminders.load()
        stale = []
        for tasklist_id, tasks in fetched.items():
            self.tasks.update({task.id: task for task in tasks})
            ids = {task.id for task in tasks}
            stale += [task_id for task_id, entry in saved.items()
                      if entry["tasklist"] == tasklist_id and task_id not in ids]
        if stale:  # the tasks were deleted elsewhere
            for task_id in stale:
                del saved[task_id]
            reminders.save(saved)
        self.check()

    def check(self) -> None:
        saved = reminders.load()
        now = datetime.now()
        notified = False
        for task_id, entry in saved.items():
            task = self.tasks.get(task_id)
            if task is None or task.done or task.due_date is None:
                continue
            when = reminders.moment(task.due_date, entry["time"])
            key = when.isoformat(timespec="minutes")
            if when <= now and entry.get("notified") != key:
                entry["notified"] = key
                notified = True
                # A timeout of 0 keeps the notification until it is dismissed, where the server allows it.
                self.tray.showMessage(f"⏰ {task.title}", task.notes or f"Due {when:%H:%M}",
                                      QSystemTrayIcon.MessageIcon.Information, 0)
        if notified:
            reminders.save(saved)


def run() -> int:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if os.environ.get("TASKLING_DEBUG") else logging.INFO)
    app = QApplication(sys.argv)
    app.setApplicationName("Taskling")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("taskling: no system tray available", file=sys.stderr)
        return 1

    worker = Worker()
    window = TaskWindow(worker)
    tray = QSystemTrayIcon(make_icon())
    tray.setToolTip("Taskling")
    notifier = ReminderNotifier(worker, tray)
    window.tasks_loaded.connect(notifier.remember)
    window.open_count_changed.connect(
        lambda count: tray.setToolTip(f"Taskling: {count} open task{'s' if count != 1 else ''}"))
    tray.activated.connect(
        lambda reason: window.toggle_at(tray.geometry())
        if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.messageClicked.connect(lambda: window.isVisible() or window.toggle_at(tray.geometry()))

    def refresh() -> None:
        window.refresh()
        notifier.reload()

    def signed_in(result, error) -> None:
        tray.showMessage("Taskling", str(error) if error else "Signed in to Google.")
        if not error:
            refresh()

    menu = QMenu()
    menu.addAction("Open", lambda: window.toggle_at(tray.geometry()))
    menu.addAction("Refresh", refresh)
    menu.addAction("Sign in to Google…", lambda: worker.run(auth.authorize, on_done=signed_in))
    menu.addSeparator()
    menu.addAction("Quit", app.quit)
    tray.setContextMenu(menu)
    tray.show()

    if auth.is_authorized():
        refresh()
    else:
        tray.showMessage("Taskling", "Not signed in: right-click the tray icon and choose “Sign in to Google…”.")
    timer = QTimer()
    timer.timeout.connect(refresh)
    timer.start(REFRESH_INTERVAL_MS)
    return app.exec()
