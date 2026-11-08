"""`shikiri-yaku update`: installs a release wheel, verifies it, and rolls back on failure.

Only the composition root builds a Host with the process runner. The package imports the settings
and the journal, and nothing else of the project.
"""

from shikiri_yaku.update.flow import run_update
from shikiri_yaku.update.host import Host, Layout
from shikiri_yaku.update.process import pause, run_command
from shikiri_yaku.update.store import default_store

__all__ = ["Host", "Layout", "default_store", "pause", "run_command", "run_update"]
