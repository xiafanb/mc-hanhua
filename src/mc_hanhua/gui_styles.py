from __future__ import annotations

STYLESHEET = """
    QWidget#root { background: #f3f7fc; color: #17243a; font-family: 'Noto Sans SC', 'Microsoft YaHei UI', 'Segoe UI', sans-serif; font-size: 14px; }
    QFrame#card { background: #ffffff; border: 1px solid #cbdced; border-radius: 10px; }
    QFrame#sourceBox { background: #fbfffb; border: 1px solid #91cf99; border-radius: 8px; }
    QFrame#statusBox { background: #fbfffb; border: 1px solid #b9dfbd; border-radius: 8px; }
    QFrame#statCard { background: #ffffff; border: 1px solid #c9d8ea; border-radius: 7px; }
    QFrame#divider { color: #d7e2ef; background: #d7e2ef; border: none; max-height: 1px; }
    QLabel#title { font-size: 32px; font-weight: 700; color: #152238; }
    QLabel#subtitle, QLabel#muted { color: #66758a; }
    QLabel#badge { color: #216f30; background: #e8f5e9; border: 1px solid #b8ddbe; border-radius: 6px; }
    QLabel#cardTitle { font-size: 19px; font-weight: 700; color: #17243a; }
    QLabel#sectionTitle, QLabel#fieldLabel { font-size: 17px; font-weight: 700; color: #17243a; }
    QLabel#fileName { font-size: 20px; font-weight: 700; color: #17243a; }
    QLabel#plus { color: #237b35; background: #e8f5e9; border: 1px solid #b8ddbe; border-radius: 8px; font-size: 40px; font-weight: 300; }
    QLabel#statusTitle { color: #1d7130; font-size: 19px; font-weight: 700; }
    QLabel#percent { color: #17732c; font-size: 29px; font-weight: 700; min-width: 86px; }
    QLabel#statLabel { color: #1f2937; font-size: 16px; font-weight: 700; }
    QLabel#statValue { color: #1f2937; font-size: 24px; font-weight: 700; }
    QLabel#statValueAccent { color: #2563eb; font-size: 21px; font-weight: 700; }
    QLineEdit, QSpinBox, QComboBox { background: #ffffff; border: 1px solid #cbd9ea; border-radius: 6px; min-height: 38px; padding: 0 10px; color: #334155; }
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid #6ea77a; }
    QLineEdit#outputPath { border-top-right-radius: 0; border-bottom-right-radius: 0; }
    QLineEdit#logFilter { min-height: 30px; min-width: 140px; font-size: 13px; }
    QPushButton { border-radius: 6px; min-height: 38px; padding: 0 16px; }
    QPushButton#primaryButton { background: #277a36; color: #ffffff; border: 1px solid #226c30; font-weight: 700; }
    QPushButton#primaryButton:hover { background: #216f30; }
    QPushButton#primaryButton:disabled { background: #9fc7a6; border-color: #9fc7a6; }
    QPushButton#secondaryButton { color: #334155; background: #f7faff; border: 1px solid #cbd9ea; min-width: 94px; }
    QPushButton#secondaryButton:hover { background: #eef4f7; }
    QPushButton#linkButton { color: #2d7040; border: none; background: transparent; padding: 0 2px; min-height: 24px; }
    QPushButton#linkButton:hover { color: #1d6e2c; text-decoration: underline; }
    QPushButton#fetchButton { background: #eef8ec; color: #28773a; border: 1px solid #a9d9ae; min-width: 94px; }
    QPushButton#dialogCancel { color: #334155; background: #f7faff; border: 1px solid #cbd9ea; min-width: 104px; font-size: 15px; font-weight: 700; }
    QPushButton#dialogSave { color: #ffffff; background: #2b7e38; border: 1px solid #226c30; min-width: 110px; font-size: 15px; font-weight: 700; }
    QCheckBox { color: #334155; spacing: 8px; }
    QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #b9c8d9; border-radius: 5px; background: #ffffff; }
    QCheckBox::indicator:checked { background: #277a36; border-color: #277a36; }
    QProgressBar { background: #dce6f1; border: none; border-radius: 5px; }
    QProgressBar::chunk { background: #2b7a38; border-radius: 5px; }
    QTextEdit#log { background: #fbfdff; color: #435774; border: 1px solid #d6e1ed; border-radius: 6px; font-family: Consolas, 'Microsoft YaHei UI'; font-size: 13px; padding: 10px; }
    QToolButton#copyLog, QToolButton#dialogClose { background: #f7faff; border: 1px solid #cbd9ea; border-radius: 6px; min-width: 34px; min-height: 30px; color: #365477; }
    QDialog#connectionDialog { background: #f3f7fc; }
    QLabel#dialogTitle { font-size: 23px; font-weight: 700; color: #17243a; }
    QScrollArea#taskScroll { background: transparent; border: none; }
    QTabWidget#rightTabs::pane { border: none; background: white; }
    QTabBar::tab { background: white; color: #66758a; border: none; border-bottom: 2px solid transparent; padding: 12px 18px; margin-right: 4px; }
    QTabBar::tab:selected { color: #216f30; background: #eef8f0; border-bottom: 2px solid #277a36; }
    QTabBar::tab:hover { background: #f3f8f5; }
    QTableWidget { background: #ffffff; alternate-background-color: #f8fbfd; color: #334155; border: 1px solid #d6e1ed; border-radius: 6px; selection-background-color: #e8f5e9; selection-color: #216f30; }
    QTableWidget::item { padding: 8px; border-bottom: 1px solid #edf2f7; }
    QHeaderView::section { background: #f3f7fc; color: #53677e; border: none; border-bottom: 1px solid #d6e1ed; padding: 10px 8px; }
    QComboBox::drop-down { border: none; width: 25px; }
    QComboBox QAbstractItemView { background: white; color: #334155; selection-background-color: #e8f5e9; selection-color: #216f30; border: 1px solid #cbd9ea; }
    QPushButton:disabled { color: #94a3b8; }
    QSplitter#workspaceSplitter::handle { background: transparent; }
    QSplitter#workspaceSplitter { border: none; }
    QFrame#card > QWidget { background: transparent; }
    QTableWidget { font-size: 15px; }
    QTableWidget::item { padding: 10px 12px; }
    QHeaderView::section { font-size: 15px; padding: 12px 10px; }
    QScrollBar:vertical { width: 10px; background: #f3f7fc; margin: 2px; }
    QScrollBar::handle:vertical { background: #cbd8e5; border-radius: 5px; min-height: 32px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
