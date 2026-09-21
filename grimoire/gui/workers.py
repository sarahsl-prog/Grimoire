"""Run blocking client calls off the GUI thread.

Qt's event loop is single-threaded: any call that waits on the network from
the GUI thread freezes the window.  Everything the client does goes through
an ApiWorker on a QThreadPool instead, and the result comes back as a signal
delivered on the GUI thread.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import shiboken6
from loguru import logger
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, SignalInstance, Slot

from grimoire.gui.errors import GuiError


class WorkerSignals(QObject):
    """Signals emitted by an ApiWorker.

    QRunnable is not a QObject, so the signals live on this companion.
    """

    finished = Signal(object)
    failed = Signal(object)
    # Emitted after finished/failed, from a `finally`, regardless of which
    # of the two fired or whether its connected slot raised.  Used purely
    # for worker lifetime bookkeeping (see `_active_workers` below), never
    # by callers.
    done = Signal()


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
        """Execute the call, emitting finished or failed exactly once.

        `done` always follows, from a `finally`, so lifetime bookkeeping in
        `run_api_call` fires even if `finished`/`failed` has no connected
        slot yet or a connected slot raises.

        Closing the window mid-request can delete `self.signals` (owned,
        transitively, by the window) out from under this pool thread before
        the call returns. Emitting on a deleted QObject raises `RuntimeError:
        Signal source has been deleted`, which would otherwise escape this
        QRunnable override and print a raw traceback to stderr - forbidden
        outright regardless of who is or isn't still listening. Every emit
        goes through `_safe_emit`, which checks liveness and also swallows
        the RuntimeError for the window between that check and the call.
        """
        try:
            try:
                result = self._fn()
            except GuiError as exc:
                self._safe_emit(self.signals.failed, exc)
            except Exception as exc:
                # An exception escaping a QRunnable kills the pool thread
                # without a word and leaves the UI spinning forever.  Log
                # the real cause, show the user something generic.
                logger.exception(f"Unexpected error in GUI worker: {exc}")
                self._safe_emit(
                    self.signals.failed,
                    GuiError("Something went wrong. See the log for details."),
                )
            else:
                self._safe_emit(self.signals.finished, result)
        finally:
            self._safe_emit(self.signals.done)

    def _safe_emit(self, signal: SignalInstance, *args: object) -> None:
        """Emit a signal unless its owning QObject has been torn down."""
        if not shiboken6.isValid(self.signals):
            return
        with contextlib.suppress(RuntimeError):
            signal.emit(*args)


# Workers currently running on the pool, keyed by identity.  QThreadPool
# does not keep a Python reference to the QRunnable it runs, and nothing
# else does either once run_api_call() returns.  Without this, a worker
# (and its WorkerSignals, which carries the connected on_ok/on_err) can be
# garbage collected before its result is delivered: reproduced reliably for
# a lambda slot (the callback simply never fires, no forced gc.collect()
# needed) and for a bound method of a plain object under gc.collect().
# Every current caller happens to pass a QWidget bound method, which Qt's
# own parent/child ownership keeps alive and which masked this bug - do not
# "clean up" this set on the assumption it is unused dead weight.
_active_workers: set[ApiWorker] = set()


def run_api_call(
    pool: QThreadPool,
    fn: Callable[[], object],
    on_ok: Callable[[Any], None],
    on_err: Callable[[GuiError], None],
) -> None:
    """Submit a client call and route its outcome to two slots.

    Holds a strong reference to the worker in `_active_workers` for the
    lifetime of the call (see that module-level comment for why) so that
    any callable slot - lambda or bound method of any object, not just a
    QWidget - reliably receives its result.

    Args:
        pool: The window's thread pool.
        fn: The call to make.
        on_ok: Receives the return value, on the GUI thread.
        on_err: Receives a GuiError, on the GUI thread.
    """
    worker = ApiWorker(fn)
    _active_workers.add(worker)
    worker.signals.finished.connect(on_ok)
    worker.signals.failed.connect(on_err)
    # `done` fires from a `finally` after finished/failed regardless of
    # outcome, so the worker is released even if on_ok/on_err itself
    # raises. `discard` (vs `remove`) makes the cleanup idempotent.
    #
    # Thread safety: this lambda runs on the pool thread that emits `done`,
    # but `WorkerSignals` was constructed on the GUI thread, and Qt's
    # AutoConnection compares the SIGNAL OWNER's thread affinity against
    # the emitting thread - not the receiver's - so the connection is
    # queued regardless of the slot being a plain lambda. The discard is
    # therefore always run on the GUI thread by its single event loop, and
    # queued delivery is FIFO, so `finished`/`failed` always reach on_ok/
    # on_err before `done` releases the reference. No lock needed here;
    # don't add one, and don't copy this pattern where that affinity
    # guarantee doesn't hold.
    worker.signals.done.connect(lambda: _active_workers.discard(worker))
    pool.start(worker)
