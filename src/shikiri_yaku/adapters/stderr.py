"""The Reporter that writes to stderr: the fallback when the journal cannot keep a fault."""

from __future__ import annotations

import sys


class StderrReporter:
    """Writes each line to stderr. Lines carry metadata only, never the content they describe."""

    def report(self, line: str) -> None:
        sys.stderr.write(f"{line}\n")
