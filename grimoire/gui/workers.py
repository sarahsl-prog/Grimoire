"""Run blocking client calls off the GUI thread.

Qt's event loop is single-threaded: any call that waits on the network from
the GUI thread freezes the window.  Everything the client does goes through
an ApiWorker on a QThreadPool instead, and the result comes back as a signal
delivered on the GUI thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from grimoire.gui.errors import GuiError


class WorkerSignals(QObject):
    """Signals emitted by an ApiWorker.

    QRunnable is not a QObject, so the signals live on this companion.
    """

    finished = Signal(object)
    failed = Signal(object)


class ApiWorker(QRunnable):
    """Call one function on a pool thread and report the outcome.

    Args:
        fn: Zero-argument callable, typically a lambda closing over a
            GrimoireClient method and its arguments.
    """

    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self._fn = fn
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        """Execute the call, emitting finished or failed exactly once."""
        try:
            result = self._fn()
        except GuiError as exc:
            self.signals.failed.emit(exc)
        except Exception as exc:
            # An exception escaping a QRunnable kills the pool thread without
            # a word and leaves the UI spinning forever.  Log the real cause,
            # show the user something generic.
            logger.exception(f"Unexpected error in GUI worker: {exc}")
            self.signals.failed.emit(
                GuiError("Something went wrong. See the log for details.")
            )
        else:
            self.signals.finished.emit(result)


def run_api_call(
    pool: QThreadPool,
    fn: Callable[[], object],
    on_ok: Callable[[Any], None],
    on_err: Callable[[GuiError], None],
) -> None:
    """Submit a client call and route its outcome to two slots.

    Args:
        pool: The window's thread pool.
        fn: The call to make.
        on_ok: Receives the return value, on the GUI thread.
        on_err: Receives a GuiError, on the GUI thread.
    """
    worker = ApiWorker(fn)
    worker.signals.finished.connect(on_ok)
    worker.signals.failed.connect(on_err)
    pool.start(worker)
