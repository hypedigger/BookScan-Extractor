"""Settings dialog.

The forms are generated from the configuration dataclasses rather than written out
by hand: every tunable value of the engine is therefore reachable from the
interface, and a value added to the engine shows up here without anyone having to
remember to add a widget for it.
"""

from __future__ import annotations

import copy
from dataclasses import fields, is_dataclass
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pdfextract.core import naming
from pdfextract.core.settings import AppSettings, PostProcessConfig

SECTIONS = (
    ("extraction", "Extraction"),
    ("seam", "Continuity"),
    ("pairing", "Pairing"),
    ("merge", "Merging"),
    ("postprocess", "Post-processing"),
    ("output", "Output"),
    ("review", "Validation"),
    ("trash", "Recycle bin"),
)

CHOICES = {
    "rotation_mode": ("rotate_stream", "render"),
    "fill_mode": ("white", "sampled"),
    "theme": ("dark", "light"),
}

HELP = {
    "threshold_merge": "At or above this score a pair is merged without asking.",
    "threshold_split": "At or below this score a pair is split without asking.",
    "cost_single": "Prior for exporting a page on its own; a pair wins above this score.",
    "gutter_skip_ratio": "Fraction of the page width ignored along the gutter.",
    "preload_ahead": "Pairs decoded ahead of the one on screen.",
    "template": "Tokens: " + ", ".join(sorted(f"{{{name}}}" for name in naming.KNOWN_TOKENS)),
    "jpeg_subsampling": "0 means 4:4:4. Anything else damages scanned text.",
    "enabled": "Processed sources go to the recycle bin, never a permanent delete.",
}


def _label_for(name: str) -> str:
    """Turn a field name into a readable label."""
    return name.replace("_", " ").capitalize()


class SettingsDialog(QDialog):
    """Edits a copy of the settings; the caller decides whether to keep it."""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = copy.deepcopy(settings)
        self._widgets: dict[tuple[str, str], QWidget] = {}
        self._weight_widgets: dict[str, QDoubleSpinBox] = {}

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        for attribute, title in SECTIONS:
            self.tabs.addTab(self._build_section(attribute), title)
        layout.addWidget(self.tabs)

        self.debug = QCheckBox("Show the detail of the metrics on the validation screen", self)
        self.debug.setChecked(self.settings.debug_metrics)
        layout.addWidget(self.debug)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(640, 620)

    def _build_section(self, attribute: str) -> QWidget:
        """Build the form of one configuration dataclass."""
        config = getattr(self.settings, attribute)
        page = QWidget(self)
        outer = QVBoxLayout(page)
        area = QScrollArea(page)
        area.setWidgetResizable(True)
        inner = QWidget(area)
        form = QFormLayout(inner)

        for field in fields(config):
            value = getattr(config, field.name)
            if field.name == "weights":
                form.addRow(QLabel("<b>Metric weights</b>", inner))
                for key, weight in sorted(value.items()):
                    spin = QDoubleSpinBox(inner)
                    spin.setRange(0.0, 1.0)
                    spin.setSingleStep(0.01)
                    spin.setDecimals(3)
                    spin.setValue(float(weight))
                    self._weight_widgets[key] = spin
                    form.addRow(_label_for(key), spin)
                continue
            widget = self._widget_for(field.name, value, inner)
            if widget is None:
                continue
            self._widgets[(attribute, field.name)] = widget
            form.addRow(_label_for(field.name), widget)
            if field.name in HELP:
                hint = QLabel(HELP[field.name], inner)
                hint.setWordWrap(True)
                hint.setStyleSheet("color: palette(mid); font-size: 11px;")
                form.addRow("", hint)

        area.setWidget(inner)
        outer.addWidget(area)
        if attribute == "postprocess":
            outer.addLayout(self._build_profiles(page))
        return page

    def _widget_for(self, name: str, value: Any, parent: QWidget) -> QWidget | None:
        """Return the editor matching the type of a configuration value."""
        if isinstance(value, bool):
            box = QCheckBox(parent)
            box.setChecked(value)
            return box
        if isinstance(value, int):
            integer = QSpinBox(parent)
            integer.setRange(0, 100_000)
            integer.setValue(value)
            return integer
        if isinstance(value, float):
            decimal = QDoubleSpinBox(parent)
            decimal.setRange(0.0, 10_000.0)
            decimal.setDecimals(3)
            decimal.setSingleStep(0.01)
            decimal.setValue(value)
            return decimal
        if isinstance(value, str):
            if name in CHOICES:
                combo = QComboBox(parent)
                combo.addItems(list(CHOICES[name]))
                combo.setCurrentText(value)
                return combo
            edit = QLineEdit(parent)
            edit.setText(value)
            return edit
        if isinstance(value, tuple):
            edit = QLineEdit(parent)
            edit.setText(", ".join(str(item) for item in value))
            return edit
        return None  # dictionaries of profiles are handled by their own controls

    def _build_profiles(self, parent: QWidget) -> QHBoxLayout:
        """Build the profile selector of the post-processing section."""
        row = QHBoxLayout()
        self.profile_combo = QComboBox(parent)
        self.profile_combo.addItem("(inline settings)", "")
        for name in sorted(self.settings.profiles):
            self.profile_combo.addItem(name, name)
        index = self.profile_combo.findData(self.settings.active_profile)
        self.profile_combo.setCurrentIndex(max(0, index))
        self.profile_combo.currentIndexChanged.connect(self._load_profile)

        save = QPushButton("Save as profile…", parent)
        save.clicked.connect(self._save_profile)
        delete = QPushButton("Delete profile", parent)
        delete.clicked.connect(self._delete_profile)

        row.addWidget(QLabel("Profile", parent))
        row.addWidget(self.profile_combo, 1)
        row.addWidget(save)
        row.addWidget(delete)
        return row

    def _load_profile(self) -> None:
        """Fill the post-processing form from the selected profile."""
        name = self.profile_combo.currentData()
        config = self.settings.profiles.get(name) if name else self.settings.postprocess
        if config is None:
            return
        for field in fields(config):
            widget = self._widgets.get(("postprocess", field.name))
            value = getattr(config, field.name)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QSpinBox):
                widget.setValue(int(value))
            elif isinstance(widget, QDoubleSpinBox):
                widget.setValue(float(value))
            elif isinstance(widget, QLineEdit):
                widget.setText(str(value))

    def _save_profile(self) -> None:
        """Store the current post-processing form as a named profile."""
        name, ok = QInputDialog.getText(self, "Profile name", "Name")
        if not ok or not name.strip():
            return
        name = name.strip()
        config = PostProcessConfig(name=name)
        self._read_into(config, "postprocess")
        self.settings.profiles[name] = config
        self.profile_combo.addItem(name, name)
        self.profile_combo.setCurrentIndex(self.profile_combo.count() - 1)

    def _delete_profile(self) -> None:
        """Remove the selected profile."""
        name = self.profile_combo.currentData()
        if not name:
            return
        self.settings.profiles.pop(name, None)
        self.profile_combo.removeItem(self.profile_combo.currentIndex())

    def _read_into(self, config: Any, attribute: str) -> None:
        """Copy the widgets of a section back into a configuration object."""
        for field in fields(config):
            widget = self._widgets.get((attribute, field.name))
            if widget is None:
                continue
            current = getattr(config, field.name)
            if isinstance(widget, QCheckBox):
                setattr(config, field.name, widget.isChecked())
            elif isinstance(widget, QSpinBox):
                setattr(config, field.name, int(widget.value()))
            elif isinstance(widget, QDoubleSpinBox):
                setattr(config, field.name, float(widget.value()))
            elif isinstance(widget, QComboBox):
                setattr(config, field.name, widget.currentText())
            elif isinstance(widget, QLineEdit):
                if isinstance(current, tuple):
                    parts = tuple(
                        part.strip() for part in widget.text().split(",") if part.strip()
                    )
                    setattr(config, field.name, parts)
                else:
                    setattr(config, field.name, widget.text())

    def result_settings(self) -> AppSettings:
        """Return the edited settings."""
        for attribute, _ in SECTIONS:
            config = getattr(self.settings, attribute)
            if is_dataclass(config):
                self._read_into(config, attribute)
        for key, spin in self._weight_widgets.items():
            self.settings.seam.weights[key] = float(spin.value())
        self.settings.active_profile = self.profile_combo.currentData() or ""
        self.settings.debug_metrics = self.debug.isChecked()
        return self.settings

    def template_warnings(self) -> list[str]:
        """Return the advisory messages about the chosen file name template."""
        try:
            return naming.validate_template(self.settings.output.template)
        except naming.NamingError as exc:
            return [str(exc)]


def edit_settings(settings: AppSettings, parent: QWidget | None = None) -> AppSettings | None:
    """Open the dialog and return the edited settings, or None when cancelled."""
    dialog = SettingsDialog(settings, parent)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return dialog.result_settings()
    return None


__all__ = ["Qt", "SettingsDialog", "edit_settings"]
