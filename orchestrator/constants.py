"""Constants with no runtime behavior. Pinned version numbers, schema
versions, and path conventions live here so every other module imports
one source of truth."""

from pathlib import Path
from typing import Final

# Claude Code minimum version; ADR-0002 D2.
MIN_CLAUDE_CODE_VERSION: Final[str] = "2.1.91"

# Schema version for every .huragok/*.yaml file; ADR-0002 D3.
SCHEMA_VERSION: Final[int] = 1

# Directory name used as the anchor for walk-up resolution; ADR-0002 D5.
HURAGOK_DIR: Final[str] = ".huragok"

# Relative paths inside .huragok/, as Path objects for composition.
STATE_FILE: Final[Path] = Path("state.yaml")
BATCH_FILE: Final[Path] = Path("batch.yaml")
DECISIONS_FILE: Final[Path] = Path("decisions.md")
WORK_DIR: Final[Path] = Path("work")
AUDIT_DIR: Final[Path] = Path("audit")
LOGS_DIR: Final[Path] = Path("logs")
RETROSPECTIVES_DIR: Final[Path] = Path("retrospectives")
REQUESTS_DIR: Final[Path] = Path("requests")
EXAMPLES_DIR: Final[Path] = Path("examples")
RATE_LIMIT_LOG: Final[Path] = Path("rate-limit-log.yaml")
DAEMON_PID_FILE: Final[Path] = Path("daemon.pid")

# Task-folder file names.
SPEC_FILE: Final[str] = "spec.md"
IMPLEMENTATION_FILE: Final[str] = "implementation.md"
TESTS_FILE: Final[str] = "tests.md"
REVIEW_FILE: Final[str] = "review.md"
UI_REVIEW_FILE: Final[str] = "ui-review.md"
STATUS_FILE: Final[str] = "status.yaml"
