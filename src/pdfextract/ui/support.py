"""Supporting the project: the Ko-fi link and the occasional invitation.

The application is free. It says so once, at the tenth launch, then every
hundredth; a tick box turns the invitation off for good.
"""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QMessageBox, QWidget

KOFI_URL = "https://ko-fi.com/hypedigger"

FIRST_PROMPT = 10  # launches before the first invitation
PROMPT_EVERY = 100  # and how often it comes back afterwards
PROMPT_DELAY_MS = 2500  # the window is up and settled before anything is asked


def open_kofi() -> None:
    """Open the Ko-fi page of the project in the default browser."""
    QDesktopServices.openUrl(QUrl(KOFI_URL))


def support_due(usage_count: int, optout: bool) -> bool:
    """Return True when this launch is one of those that invite to support."""
    if optout:
        return False
    if usage_count == FIRST_PROMPT:
        return True
    return usage_count >= PROMPT_EVERY and usage_count % PROMPT_EVERY == 0


class SupportDialog(QMessageBox):
    """The invitation: one click sends it away, one tick keeps it away."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Enjoying BookScan Extractor?")
        self.setIcon(QMessageBox.Icon.NoIcon)
        self.setText("☕  Enjoying BookScan Extractor?")
        self.setInformativeText(
            "This app is free and open source, built in its creator's spare time.\n\n"
            "If it saves you time, a small coffee on Ko-fi helps keep it going — "
            "thank you!"
        )
        self.optout_box = QCheckBox("Don't ask me again", self)
        self.setCheckBox(self.optout_box)
        self.kofi_button = self.addButton(
            "☕  Support on Ko-fi", QMessageBox.ButtonRole.AcceptRole
        )
        self.kofi_button.setProperty("kind", "primary")
        self.later_button = self.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        self.setDefaultButton(self.kofi_button)
        self.setEscapeButton(self.later_button)

    @property
    def support_chosen(self) -> bool:
        """Return True when the dialog was closed on the Ko-fi button."""
        return self.clickedButton() is self.kofi_button

    @property
    def optout_chosen(self) -> bool:
        """Return True when the user asked not to be invited again."""
        return self.optout_box.isChecked()
