"""设置：主题、强调色、背景图片、模板与安全校验、工作区管理。"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

from ..config import BackgroundConfig, Settings
from ..domain.naming import TEMPLATE_LABELS
from .theme import PALETTES, THEME_ORDER

ACCENT_PRESETS = ["#3B6EA5", "#52796F", "#7B5EA7", "#B26B00", "#C0392B", "#2C7A7B"]
ALIGNMENTS = [("居中", "center"), ("左上", "top-left"), ("上中", "top"), ("右上", "top-right"),
              ("左中", "left"), ("右中", "right"), ("左下", "bottom-left"),
              ("下中", "bottom"), ("右下", "bottom-right")]
DISPLAYS = [("填满裁切（默认）", "fill"), ("完整显示", "fit"), ("拉伸", "stretch")]

class SettingsDialog(QDialog):
    previewChanged = Signal(object)     # Settings（用于即时预览主题）

    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.settings = Settings.from_dict(service.settings.to_dict())
        self.paths = service.paths
        self.setWindowTitle("设置")
        self.resize(720, 700)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        title = QLabel("外观与整理设置")
        title.setObjectName("Title")
        outer.addWidget(title)

        # 内容区滚动：规则开关较多，窗口小时“应用/关闭”仍固定在底部可见
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(12)

        # 主题
        theme_box = QGroupBox("主题")
        theme_layout = QVBoxLayout(theme_box)
        row = QHBoxLayout()
        self.theme_buttons: dict[str, QPushButton] = {}
        for key in THEME_ORDER:
            button = QPushButton(PALETTES[key].name)
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, k=key: self._set_theme(k))
            self.theme_buttons[key] = button
            row.addWidget(button)
        row.addStretch(1)
        theme_layout.addLayout(row)

        accent_row = QHBoxLayout()
        accent_row.addWidget(QLabel("强调色"))
        self.accent_button = QPushButton("选择颜色…")
        self.accent_button.clicked.connect(self._pick_accent)
        accent_row.addWidget(self.accent_button)
        for preset in ACCENT_PRESETS:
            chip = QPushButton("")
            chip.setFixedSize(22, 22)
            chip.setStyleSheet(f"background-color: {preset}; border-radius: 11px;"
                               f"border: 1px solid rgba(0,0,0,0.15);")
            chip.clicked.connect(lambda _=False, c=preset: self._set_accent(c))
            accent_row.addWidget(chip)
        accent_row.addStretch(1)
        theme_layout.addLayout(accent_row)
        layout.addWidget(theme_box)

        # 背景
        bg_box = QGroupBox("自定义背景")
        bg_form = QFormLayout(bg_box)
        self.bg_name = QLineEdit()
        self.bg_name.setReadOnly(True)
        pick_row = QHBoxLayout()
        pick_row.addWidget(self.bg_name, 1)
        pick_btn = QPushButton("选择图片…")
        pick_btn.clicked.connect(self._pick_background)
        remove_btn = QPushButton("移除")
        remove_btn.setProperty("kind", "ghost")
        remove_btn.clicked.connect(self._remove_background)
        reset_btn = QPushButton("恢复默认")
        reset_btn.setProperty("kind", "ghost")
        reset_btn.clicked.connect(self._reset_background)
        pick_row.addWidget(pick_btn)
        pick_row.addWidget(remove_btn)
        pick_row.addWidget(reset_btn)
        holder = QWidget()
        holder.setLayout(pick_row)
        bg_form.addRow("图片", holder)

        self.display_combo = QComboBox()
        for label, value in DISPLAYS:
            self.display_combo.addItem(label, value)
        self.display_combo.currentIndexChanged.connect(self._emit_preview)
        bg_form.addRow("显示方式", self.display_combo)

        self.align_combo = QComboBox()
        for label, value in ALIGNMENTS:
            self.align_combo.addItem(label, value)
        self.align_combo.currentIndexChanged.connect(self._emit_preview)
        bg_form.addRow("对齐位置", self.align_combo)

        self.intensity = self._slider(bg_form, "背景强度")
        self.mask = self._slider(bg_form, "遮罩深浅")
        self.panel_opacity = self._slider(bg_form, "面板透明度", minimum=50)
        layout.addWidget(bg_box)

        # 整理规则
        rule_box = QGroupBox("整理规则")
        rule_form = QFormLayout(rule_box)
        self.template_combo = QComboBox()
        for key, label in TEMPLATE_LABELS.items():
            self.template_combo.addItem(f"{key}：{label}", key)
        rule_form.addRow("散曲目录模板", self.template_combo)
        self.digits_spin = QSpinBox()
        self.digits_spin.setRange(1, 4)
        rule_form.addRow("曲号位数", self.digits_spin)
        self.verify_check = QCheckBox("复制/跨盘移动后校验文件内容（SHA-256）")
        rule_form.addRow("", self.verify_check)
        self.auto_copy_archive = QLabel("压缩包全自动处理：随同目录音频归档，或放入“原始压缩包”目录（不解压）。")
        self.auto_copy_archive.setObjectName("Muted")
        self.auto_copy_archive.setWordWrap(True)
        rule_form.addRow("", self.auto_copy_archive)
        layout.addWidget(rule_box)

        # 数据目录
        data_box = QGroupBox("数据目录")
        data_layout = QVBoxLayout(data_box)
        self.data_label = QLabel("")
        self.data_label.setWordWrap(True)
        self.data_label.setObjectName("Muted")
        data_layout.addWidget(self.data_label)
        open_btn = QPushButton("打开数据目录")
        open_btn.setProperty("kind", "ghost")
        open_btn.clicked.connect(self._open_data_dir)
        data_layout.addWidget(open_btn)
        layout.addWidget(data_box)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        buttons = QDialogButtonBox()
        apply_btn = buttons.addButton("应用", QDialogButtonBox.ApplyRole)
        apply_btn.setProperty("kind", "primary")
        close_btn = buttons.addButton("关闭", QDialogButtonBox.RejectRole)
        apply_btn.clicked.connect(self._apply)
        close_btn.clicked.connect(self.reject)
        outer.addWidget(buttons)
        self.apply_button = apply_btn

        self._load_into_widgets()

    # ------------------------------------------------------------------
    def _slider(self, form: QFormLayout, label: str, minimum: int = 0) -> QSlider:
        slider = QSlider(Qt.Horizontal)
        slider.setRange(minimum, 100)
        slider.setSingleStep(1)
        slider.valueChanged.connect(self._emit_preview)
        form.addRow(label, slider)
        return slider

    def _load_into_widgets(self) -> None:
        self._loading = True
        try:
            self._load_into_widgets_impl()
        finally:
            self._loading = False

    def _load_into_widgets_impl(self) -> None:
        s = self.settings
        for key, button in self.theme_buttons.items():
            button.setChecked(key == s.theme)
        self._update_accent_button()
        bg = s.background
        self.bg_name.setText(Path(bg.image).name if bg.image else "（未设置）")
        index = self.display_combo.findData(bg.display)
        self.display_combo.setCurrentIndex(max(0, index))
        index = self.align_combo.findData(bg.align)
        self.align_combo.setCurrentIndex(max(0, index))
        self.intensity.setValue(int(bg.intensity * 100))
        self.mask.setValue(int(bg.mask * 100))
        self.panel_opacity.setValue(int(bg.panel_opacity * 100))
        index = self.template_combo.findData(s.template)
        self.template_combo.setCurrentIndex(max(0, index))
        self.digits_spin.setValue(s.track_digits)
        self.verify_check.setChecked(s.verify_hash)
        self.data_label.setText(f"数据目录：{self.paths.describe()}")

    def _update_accent_button(self) -> None:
        self.accent_button.setStyleSheet(
            f"background-color: {self.settings.accent}; color: white; border-radius: 6px;")
        self.accent_button.setText(self.settings.accent.upper())

    def _current_theme(self) -> str:
        for key, button in self.theme_buttons.items():
            if button.isChecked():
                return key
        return "light"

    def _set_theme(self, key: str) -> None:
        for k, button in self.theme_buttons.items():
            button.setChecked(k == key)
        self.settings.theme = key
        self._emit_preview()

    def _set_accent(self, color: str) -> None:
        self.settings.accent = color
        self._update_accent_button()
        self._emit_preview()

    def _pick_accent(self) -> None:
        color = QColorDialog.getColor(QtGui_color(self.settings.accent), self, "选择强调色")
        if color.isValid():
            self._set_accent(color.name())

    def _pick_background(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择背景图片", "",
                                              "图片 (*.jpg *.jpeg *.png)")
        if not path:
            return
        suffix = Path(path).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png"):
            QMessageBox.warning(self, "格式不支持", "第一版支持 JPG / JPEG / PNG 背景图片。")
            return
        target_dir = self.paths.background_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"background{suffix}"
        try:
            shutil.copy2(path, target)
        except OSError as exc:
            QMessageBox.warning(self, "复制失败", f"无法复制背景图片：{exc}")
            return
        self.settings.background.image = target.name
        self.bg_name.setText(target.name)
        self._emit_preview()

    def _remove_background(self) -> None:
        self.settings.background = BackgroundConfig()
        self.bg_name.setText("（未设置）")
        self._emit_preview()

    def _reset_background(self) -> None:
        self.settings.background = BackgroundConfig()
        self.bg_name.setText("（未设置）")
        self.display_combo.setCurrentIndex(0)
        self.align_combo.setCurrentIndex(0)
        self.intensity.setValue(55)
        self.mask.setValue(35)
        self.panel_opacity.setValue(90)
        self._emit_preview()

    def _collect(self) -> Settings:
        s = self.settings
        s.theme = self._current_theme()
        bg = s.background
        bg.display = self.display_combo.currentData() or "fill"
        bg.align = self.align_combo.currentData() or "center"
        bg.intensity = self.intensity.value() / 100
        bg.mask = self.mask.value() / 100
        bg.panel_opacity = self.panel_opacity.value() / 100
        s.template = self.template_combo.currentData() or "A"
        s.track_digits = self.digits_spin.value()
        s.verify_hash = self.verify_check.isChecked()
        return s

    def _emit_preview(self) -> None:
        self.previewChanged.emit(self._collect())

    def _apply(self) -> None:
        settings = self._collect()
        self.service.settings = settings
        self.service.save_settings()
        self.service.save_state()
        self.previewChanged.emit(settings)
        self._load_into_widgets()

    def _open_data_dir(self) -> None:
        import os
        os.startfile(str(self.paths.root))  # noqa: S606


def QtGui_color(value: str):
    from PySide6.QtGui import QColor
    return QColor(value)
