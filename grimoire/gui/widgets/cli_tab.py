"""CLI runner tab.

Runs a fixed set of read-only ``grimoire`` subcommands through QProcess and
streams their output.  There is no shell: the program is an executable path
and the arguments are an argv list, so nothing typed into the arguments field
can chain a second command.
"""

from __future__ import annotations

import shlex
import shutil
import sys

from loguru import logger
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# Read-only commands only.  `keys` issues credentials, `migrate` mutates the
# schema, `reindex` is a long rewrite, `watch` starts a daemon outside this
# window's lifecycle, and `ingest` belongs to the Ingest tab.  All of them
# stay available in a terminal, where their consequences are visible.
ALLOWED_COMMANDS: dict[str, list[str]] = {
    "status": ["status"],
    "status --detailed": ["status", "--detailed"],
    "search": ["search"],
    "ask": ["ask"],
    "docs": ["docs"],
    "config show": ["config", "show"],
    "cache stats": ["cache", "stats"],
    "categories list": ["categories", "list"],
}

_MAX_OUTPUT_BLOCKS = 5000
_TERMINATE_GRACE_MS = 2000


class CliTab(QWidget):
    """Run an allowlisted grimoire command and watch its output."""

    def __init__(self) -> None:
        super().__init__()
        self._process: QProcess | None = None

        self.command_combo = QComboBox()
        self.command_combo.addItems(list(ALLOWED_COMMANDS))
        self.args_field = QLineEdit()
        self.args_field.setPlaceholderText("Extra arguments, e.g. --limit 5")
        self.run_button = QPushButton("Run")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)

        self.output_view = QTextEdit()
        self.output_view.setReadOnly(True)
        self.output_view.document().setMaximumBlockCount(_MAX_OUTPUT_BLOCKS)
        self.output_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

        controls = QHBoxLayout()
        controls.addWidget(self.command_combo)
        controls.addWidget(self.args_field, stretch=1)
        controls.addWidget(self.run_button)
        controls.addWidget(self.stop_button)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.output_view, stretch=1)

        self.run_button.clicked.connect(self.run)
        self.stop_button.clicked.connect(self.stop)

    # -- argv --------------------------------------------------------------

    @staticmethod
    def _program() -> tuple[str, list[str]]:
        """Locate the grimoire executable.

        Falls back to the running interpreter's module entry point, which is
        what a source checkout without an installed console script has.
        """
        executable = shutil.which("grimoire")
        if executable:
            return executable, []
        return sys.executable, ["-m", "grimoire.cli.main"]

    def build_argv(self) -> list[str]:
        """Assemble the full argv for the selected command.

        The subcommand always comes from the dropdown and is placed before
        anything the user typed, so a typed subcommand cannot displace it.
        """
        _, prefix = self._program()
        subcommand = ALLOWED_COMMANDS[self.command_combo.currentText()]
        extra = shlex.split(self.args_field.text())
        return [*prefix, *subcommand, *extra]

    # -- process lifecycle -------------------------------------------------

    def run(self) -> None:
        """Start the selected command."""
        if self._process is not None:
            return
        try:
            argv = self.build_argv()
        except ValueError as exc:
            # shlex raises on an unbalanced quote; that is user input, not a
            # bug, so it belongs in the output view.
            self._append(f"Could not parse arguments (check your quotes): {exc}")
            return
        program, _ = self._program()
        self.start_process(program, argv)

    def start_process(self, program: str, argv: list[str]) -> None:
        """Launch a process and stream its output.

        Args:
            program: Executable path.  Never a shell.
            argv: Arguments, already split into separate entries.
        """
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._drain_output)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_process_error)
        self._process = process

        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._append(f"$ {program} {' '.join(argv)}")
        process.start(program, argv)

    def stop(self) -> None:
        """Ask the process to exit, then kill it if it ignores the request."""
        process = self._process
        if process is None:
            return
        process.terminate()
        if not process.waitForFinished(_TERMINATE_GRACE_MS):
            self._append("Process ignored terminate; killing it.")
            process.kill()
            process.waitForFinished(_TERMINATE_GRACE_MS)

    def shutdown(self) -> None:
        """Called by the main window before the application closes."""
        self.stop()

    def _drain_output(self) -> None:
        process = self._process
        if process is None:
            return
        chunk = bytes(process.readAllStandardOutput().data()).decode(
            "utf-8", errors="replace"
        )
        if chunk:
            self._append(chunk.rstrip("\n"))

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._drain_output()
        self._append(f"— finished with exit code {exit_code} —")
        self._process = None
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        logger.warning(f"CLI process error: {error}")
        if self._process is None or error != QProcess.ProcessError.FailedToStart:
            # A process that started and then crashed or was killed by
            # stop() also reaches this handler, but Qt emits `finished` for
            # those too, which already reports the real outcome. Only a
            # genuine failure to launch belongs here, so nothing here
            # duplicates or contradicts what `_on_finished` says.
            return
        self._append("Could not start the command. Is grimoire on your PATH?")
        self._process = None
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _append(self, text: str) -> None:
        self.output_view.append(text)
