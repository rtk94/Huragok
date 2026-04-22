"""The asyncio event loop that orchestrates a batch (ADR-0002 D1).

:mod:`orchestrator.supervisor.loop` holds the top-level event loop and
state-machine driver. :mod:`orchestrator.supervisor.signals` handles
SIGTERM / SIGUSR1 and watches the request-file directory.
:mod:`orchestrator.supervisor.sd_notify` is a stdlib-only implementation
of systemd's notify protocol (a no-op when ``NOTIFY_SOCKET`` is unset).
"""

from orchestrator.supervisor.loop import (
    ROLE_FOR_STATE,
    SessionAttempt,
    SupervisorContext,
    build_dispatcher,
    run,
    run_supervisor,
)
from orchestrator.supervisor.sd_notify import sd_notify
from orchestrator.supervisor.signals import (
    SignalState,
    install_signal_handlers,
    process_request_files,
)

__all__ = [
    "ROLE_FOR_STATE",
    "SessionAttempt",
    "SignalState",
    "SupervisorContext",
    "build_dispatcher",
    "install_signal_handlers",
    "process_request_files",
    "run",
    "run_supervisor",
    "sd_notify",
]
