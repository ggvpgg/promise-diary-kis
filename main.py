import json
import os
import sys
import tempfile
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSplitter, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)


DATA_DIR = Path(os.getenv("PROMISE_DIARY_DATA_DIR") or
                Path.home() / "Library/Application Support/PromiseDiary")


def stamp():
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".tmp-", suffix=".json", delete=False) as file:
            name = file.name
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def read_cases(folder):
    cases, damaged = {}, []
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(item, dict) or item["id"] != path.stem:
                raise ValueError("неверный идентификатор")
            if not isinstance(item["text"], str) or not item["text"].strip():
                raise ValueError("пустой текст")
            date.fromisoformat(item["promise_date"])
            date.fromisoformat(item["due_date"])
            if item["status"] not in ("active", "completed") or not isinstance(item["comments"], list):
                raise ValueError("неверное состояние или комментарии")
            for comment in item["comments"]:
                if not isinstance(comment["text"], str) or not comment["text"].strip():
                    raise ValueError("повреждённый комментарий")
                datetime.fromisoformat(comment["created_at"])
            cases[item["id"]] = item
        except (OSError, ValueError, KeyError, TypeError) as exc:
            damaged.append(f"{path.name}: {exc}")
    return cases, damaged


def search_key(text):
    return unicodedata.normalize("NFC", text).casefold().replace("ё", "е")


def app_icon():
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#355f49"))
    painter.drawRoundedRect(4, 4, 40, 40, 7, 7)
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setPixelSize(29)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "✓")
    painter.end()
    return QIcon(pixmap)


class Diary(QMainWindow):
    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self.cases, self.damaged = read_cases(folder)
        self.current_id = None
        self.dirty = False
        self.loading = False
        self.setWindowTitle("Дневник обещаний")
        self.setWindowIcon(app_icon())
        self.resize(1120, 700)
        self.setMinimumSize(850, 560)
        self.build_ui()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.save_draft)
        self.refresh()
        if self.draft_path(None).exists() or not self.cases:
            self.new_case(first=True)
        else:
            self.select_case(next(iter(self.cases)))
        if self.damaged:
            QTimer.singleShot(0, self.warn_damaged)

    def build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 10)
        outer.setSpacing(10)

        tools = QFrame()
        tools.setObjectName("tools")
        bar = QVBoxLayout(tools)
        bar.setContentsMargins(11, 9, 11, 9)
        bar.setSpacing(8)
        first = QHBoxLayout()
        first.addWidget(QLabel("Поиск"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Текст дела или комментария")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        first.addWidget(self.search, 1)
        add = QPushButton("+ Новое дело")
        add.setObjectName("primary")
        add.clicked.connect(self.new_case)
        first.addWidget(add)
        bar.addLayout(first)
        second = QHBoxLayout()
        second.addWidget(QLabel("Показать"))
        self.period = QComboBox()
        self.period.addItems(["Все дела", "День", "Неделя", "Месяц"])
        self.period.currentIndexChanged.connect(self.refresh)
        second.addWidget(self.period)
        second.addWidget(QLabel("по сроку выполнения на дату"))
        self.day = QDateEdit(QDate.currentDate())
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("dd.MM.yyyy")
        self.day.dateChanged.connect(self.refresh)
        second.addWidget(self.day)
        today = QPushButton("Сегодня")
        today.clicked.connect(lambda: self.day.setDate(QDate.currentDate()))
        second.addWidget(today)
        second.addStretch()
        bar.addLayout(second)
        outer.addWidget(tools)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)
        self.count = QLabel("Дела")
        self.count.setObjectName("heading")
        lv.addWidget(self.count)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Состояние", "Срок", "Обещано", "Дело", "Комм."])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(31)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        for column in (0, 1, 2, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.table_selected)
        lv.addWidget(self.table)

        right = QFrame()
        right.setObjectName("editor")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(12, 10, 12, 10)
        rv.setSpacing(7)
        self.title = QLabel("Дело")
        self.title.setObjectName("heading")
        rv.addWidget(self.title)
        self.draft_note = QLabel("Черновик сохранён")
        self.draft_note.setObjectName("draftNote")
        self.draft_note.hide()
        rv.addWidget(self.draft_note)

        dates = QHBoxLayout()
        for label, attr in (("Дата обещания", "promised"), ("Срок выполнения", "due")):
            box = QVBoxLayout()
            box.addWidget(QLabel(label))
            widget = QDateEdit(QDate.currentDate())
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("dd.MM.yyyy")
            widget.dateChanged.connect(self.changed)
            setattr(self, attr, widget)
            box.addWidget(widget)
            dates.addLayout(box)
        rv.addLayout(dates)
        rv.addWidget(QLabel("Что обещано"))
        self.body = QTextEdit()
        self.body.setPlaceholderText("Кому и что было обещано")
        self.body.setMinimumHeight(100)
        self.body.setMaximumHeight(150)
        self.body.textChanged.connect(self.changed)
        rv.addWidget(self.body)
        self.done = QCheckBox("Дело выполнено")
        self.done.toggled.connect(self.changed)
        rv.addWidget(self.done)
        actions = QHBoxLayout()
        save = QPushButton("Сохранить дело")
        save.setObjectName("primary")
        save.clicked.connect(self.save_case)
        actions.addWidget(save)
        self.delete = QPushButton("Удалить")
        self.delete.setObjectName("danger")
        self.delete.clicked.connect(self.delete_case)
        actions.addWidget(self.delete)
        rv.addLayout(actions)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        rv.addWidget(line)
        self.comments_title = QLabel("Комментарии")
        self.comments_title.setObjectName("heading")
        rv.addWidget(self.comments_title)
        self.comments = QListWidget()
        rv.addWidget(self.comments, 1)
        comment_row = QHBoxLayout()
        self.comment_input = QLineEdit()
        self.comment_input.setPlaceholderText("Добавить комментарий")
        self.comment_input.textChanged.connect(self.changed)
        self.comment_input.returnPressed.connect(self.add_comment)
        comment_row.addWidget(self.comment_input, 1)
        self.comment_button = QPushButton("Добавить")
        self.comment_button.clicked.connect(self.add_comment)
        comment_row.addWidget(self.comment_button)
        rv.addLayout(comment_row)

        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([650, 450])
        outer.addWidget(split, 1)
        self.statusBar().showMessage("Данные хранятся локально")

    def draft_path(self, case_id):
        return self.folder / "drafts" / f"{case_id or 'draft_new_promise'}.json"

    def read_draft(self, case_id):
        try:
            draft = json.loads(self.draft_path(case_id).read_text(encoding="utf-8"))
            if not isinstance(draft, dict):
                return None
            if case_id in self.cases and draft.get("saved_at"):
                saved = datetime.fromisoformat(draft["saved_at"])
                updated = datetime.fromisoformat(self.cases[case_id]["updated_at"])
                if saved <= updated:
                    return None
            return draft.get("data", draft)
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def form(self):
        return {"promise_id": self.current_id, "text": self.body.toPlainText(),
                "promise_date": self.promised.date().toString("yyyy-MM-dd"),
                "due_date": self.due.date().toString("yyyy-MM-dd"),
                "status": "completed" if self.done.isChecked() else "active",
                "pending_comment": self.comment_input.text(), "saved_at": stamp()}

    def changed(self, *_):
        if not self.loading:
            self.dirty = True
            self.timer.start()

    def save_draft(self):
        if not self.dirty:
            return True
        try:
            write_json(self.draft_path(self.current_id), self.form())
            self.draft_note.setText("Черновик сохранён")
            self.draft_note.show()
            self.dirty = False
            return True
        except OSError as exc:
            self.draft_note.setText("Не удалось сохранить черновик")
            self.draft_note.show()
            self.statusBar().showMessage(f"Ошибка записи: {exc}")
            return False

    def fill(self, item, draft=None):
        self.loading = True
        value = draft or item
        self.body.setPlainText(value.get("text", ""))
        for widget, key in ((self.promised, "promise_date"), (self.due, "due_date")):
            parsed = QDate.fromString(value.get(key, date.today().isoformat()), "yyyy-MM-dd")
            widget.setDate(parsed if parsed.isValid() else QDate.currentDate())
        self.done.setChecked(value.get("status") == "completed")
        self.comment_input.setText(value.get("pending_comment", ""))
        self.comments.clear()
        for comment in item.get("comments", []):
            when = datetime.fromisoformat(comment["created_at"]).astimezone().strftime("%d.%m.%Y %H:%M")
            self.comments.addItem(f"{when}  ·  {comment['text']}")
        self.comments_title.setText(f"Комментарии ({len(item.get('comments', []))})")
        self.draft_note.setVisible(draft is not None)
        self.draft_note.setText("Восстановлен черновик" if draft else "")
        self.delete.setEnabled(self.current_id is not None)
        self.comment_input.setEnabled(self.current_id is not None)
        self.comment_button.setEnabled(self.current_id is not None)
        self.title.setText("Новое дело" if self.current_id is None else "Редактирование дела")
        self.loading = False
        self.dirty = False

    def new_case(self, first=False):
        if not first and not self.save_draft():
            QMessageBox.warning(self, "Черновик", "Не удалось сохранить ввод. Повторите попытку.")
            return
        self.current_id = None
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.blockSignals(False)
        self.fill({}, self.read_draft(None))

    def select_case(self, case_id):
        self.current_id = case_id
        self.fill(self.cases[case_id], self.read_draft(case_id))
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == case_id:
                self.table.selectRow(row)
                break
        self.table.blockSignals(False)

    def table_selected(self):
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            return
        case_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if case_id == self.current_id:
            return
        if not self.save_draft():
            self.table.blockSignals(True)
            self.table.clearSelection()
            self.table.blockSignals(False)
            QMessageBox.warning(self, "Черновик", "Не удалось сохранить ввод. Вы остались в прежнем деле.")
            return
        self.select_case(case_id)

    def refresh(self, *_):
        query = search_key(self.search.text().strip())
        selected = self.day.date().toPython()
        period = self.period.currentIndex()
        cases = sorted(self.cases.values(), key=lambda x: (x["status"] == "completed", x["due_date"]))
        shown = []
        for item in cases:
            due = date.fromisoformat(item["due_date"])
            if period == 1 and due != selected:
                continue
            if period == 2 and not (selected - timedelta(days=selected.weekday()) <= due <
                                    selected - timedelta(days=selected.weekday()) + timedelta(days=7)):
                continue
            if period == 3 and (due.year, due.month) != (selected.year, selected.month):
                continue
            if query and query not in search_key(item["text"]) and not any(
                    query in search_key(comment["text"]) for comment in item["comments"]):
                continue
            shown.append(item)
        self.table.blockSignals(True)
        self.table.setRowCount(len(shown))
        for row, item in enumerate(shown):
            state = "Выполнено" if item["status"] == "completed" else (
                "Просрочено" if item["due_date"] < date.today().isoformat() else "В работе")
            values = [state, item["due_date"], item["promise_date"],
                      item["text"].replace("\n", " "), str(len(item["comments"])) or "0"]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, item["id"])
                    cell.setForeground(QColor("#a14c42" if state == "Просрочено" else
                                              "#66796a" if state == "Выполнено" else "#355f49"))
                self.table.setItem(row, column, cell)
            if item["id"] == self.current_id:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self.count.setText(f"Дела  ·  {len(shown)} из {len(self.cases)}")
        self.statusBar().showMessage(f"Данные хранятся локально  ·  повреждённых записей: {len(self.damaged)}")

    def save_case(self):
        form = self.form()
        if not form["text"].strip():
            QMessageBox.warning(self, "Пустое дело", "Напишите, что было обещано.")
            self.body.setFocus()
            return False
        old_id = self.current_id
        case_id = old_id or str(uuid.uuid4())
        old = self.cases.get(case_id, {})
        item = {"id": case_id, "text": form["text"].strip(),
                "promise_date": form["promise_date"], "due_date": form["due_date"],
                "status": form["status"], "comments": old.get("comments", []),
                "created_at": old.get("created_at", stamp()), "updated_at": stamp()}
        try:
            write_json(self.folder / f"{case_id}.json", item)
        except OSError as exc:
            self.save_draft()
            QMessageBox.critical(self, "Ошибка сохранения", f"Дело не записано: {exc}\nВвод остался в форме.")
            return False
        self.cases[case_id] = item
        self.current_id = case_id
        self.dirty = bool(form["pending_comment"])
        try:
            self.draft_path(old_id).unlink(missing_ok=True)
        except OSError:
            pass
        if self.dirty:
            self.save_draft()
        else:
            self.draft_note.hide()
        self.refresh()
        self.select_case(case_id)
        self.statusBar().showMessage("Дело сохранено", 3000)
        return True

    def add_comment(self):
        if self.current_id is None or not self.comment_input.text().strip():
            return
        if not self.save_case():
            return
        item = dict(self.cases[self.current_id])
        item["comments"] = list(item["comments"]) + [{"id": str(uuid.uuid4()),
            "text": self.comment_input.text().strip(), "created_at": stamp()}]
        item["updated_at"] = stamp()
        try:
            write_json(self.folder / f"{self.current_id}.json", item)
        except OSError as exc:
            QMessageBox.critical(self, "Ошибка", f"Комментарий не записан: {exc}")
            return
        self.cases[self.current_id] = item
        self.loading = True
        self.comment_input.clear()
        self.loading = False
        try:
            self.draft_path(self.current_id).unlink(missing_ok=True)
        except OSError:
            pass
        self.fill(item)
        self.refresh()

    def delete_case(self):
        if self.current_id is None:
            return
        if QMessageBox.question(self, "Удалить дело", "Удалить выбранное дело?") != QMessageBox.StandardButton.Yes:
            return
        try:
            (self.folder / f"{self.current_id}.json").unlink()
            self.draft_path(self.current_id).unlink(missing_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Ошибка удаления", str(exc))
            return
        del self.cases[self.current_id]
        self.current_id = None
        self.refresh()
        if self.cases:
            self.select_case(next(iter(self.cases)))
        else:
            self.new_case(first=True)

    def warn_damaged(self):
        QMessageBox.warning(self, "Повреждённые записи",
                            "Некоторые файлы не удалось открыть; остальные дела доступны:\n\n" +
                            "\n".join(self.damaged[:8]))

    def closeEvent(self, event):
        if not self.save_draft():
            event.ignore()
            QMessageBox.warning(self, "Черновик", "Не удалось сохранить ввод. Окно осталось открытым.")
        else:
            event.ignore()
            self.hide()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, "#f4f6f3"),
                        (QPalette.ColorRole.WindowText, "#25372c"),
                        (QPalette.ColorRole.Base, "#ffffff"),
                        (QPalette.ColorRole.AlternateBase, "#f8faf7"),
                        (QPalette.ColorRole.Text, "#25372c"),
                        (QPalette.ColorRole.Button, "#f7f9f6"),
                        (QPalette.ColorRole.ButtonText, "#25372c"),
                        (QPalette.ColorRole.Highlight, "#dce9de"),
                        (QPalette.ColorRole.HighlightedText, "#25372c"),
                        (QPalette.ColorRole.PlaceholderText, "#748277")):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet("""
        QWidget { color: #25372c; font-size: 12px; }
        QWidget#root { background: #f4f6f3; }
        QFrame#tools, QFrame#editor { background: white; border: 1px solid #d9e1d9; border-radius: 5px; }
        QLabel#heading { font-size: 14px; font-weight: 700; color: #263c2f; }
        QLabel#draftNote { color: #77643c; background: #fff7e5; padding: 4px; }
        QLineEdit, QTextEdit, QListWidget, QTableWidget {
            background: white; color: #25372c; border: 1px solid #cdd8ce; border-radius: 4px;
            selection-background-color: #dce9de; selection-color: #25372c;
        }
        QLineEdit { padding: 5px 7px; }
        QTextEdit { padding: 6px; }
        QLineEdit:focus, QTextEdit:focus { border-color: #5e876b; }
        QPushButton { color: #25372c; background: #f7f9f6; border: 1px solid #cdd8ce;
                      border-radius: 4px; padding: 6px 11px; }
        QPushButton:hover { background: #eaf0e9; }
        QPushButton:disabled { color: #87968a; background: #f3f5f2; }
        QPushButton#primary { color: white; background: #355f49; border-color: #355f49; font-weight: 600; }
        QPushButton#primary:hover { background: #294b39; }
        QPushButton#danger { color: #a34b42; }
        QCheckBox { color: #25372c; spacing: 7px; }
        QTableWidget { alternate-background-color: #fafbf9; gridline-color: #edf0ed; }
        QTableWidget::item:selected { background: #dce9de; color: #25372c; }
        QHeaderView::section { background: #f0f4ef; color: #344a3b; border: none;
                               border-bottom: 1px solid #d9e1d9; padding: 7px; font-weight: 600; }
        QStatusBar { background: #f4f6f3; border-top: 1px solid #e0e7e0; }
    """)
    app.setQuitOnLastWindowClosed(False)
    window = Diary(DATA_DIR)
    tray = QSystemTrayIcon(app_icon(), app)
    tray.setToolTip("Дневник обещаний")
    menu = QMenu()
    open_action = QAction("Открыть дневник", menu)
    open_action.triggered.connect(lambda: (window.show(), window.raise_(), window.activateWindow()))
    menu.addAction(open_action)
    new_action = QAction("Новое дело", menu)
    new_action.triggered.connect(lambda: (window.show(), window.new_case()))
    menu.addAction(new_action)
    menu.addSeparator()
    quit_action = QAction("Выход", menu)
    quit_action.triggered.connect(lambda: app.quit() if window.save_draft() else window.show())
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: open_action.trigger() if reason in
                           (QSystemTrayIcon.ActivationReason.Trigger,
                            QSystemTrayIcon.ActivationReason.DoubleClick) else None)
    tray.show()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
