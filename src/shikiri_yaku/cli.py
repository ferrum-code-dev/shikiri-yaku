from __future__ import annotations

import argparse
from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_PROGRAM = "shikiri-yaku"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=_PROGRAM)
    parser.add_argument("--version", action="version", version=f"%(prog)s {version(_PROGRAM)}")
    parser.parse_args(argv)
    return 0
