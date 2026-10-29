"""The family of a task: the root its `parent_id` chain leads to, and every task under that root.

Pure: it reads the task list it is given and does no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from shikiri_yaku.ports import TaskStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.ports import ShikiruTask


@dataclass(frozen=True, slots=True)
class Family:
    root: ShikiruTask
    members: tuple[ShikiruTask, ...]

    @property
    def settled(self) -> bool:
        """Whether no member is queued, running, integrating or escalated."""
        return not any(task.status in TaskStatus.ACTIVE for task in self.members)


def family_of(tasks: Sequence[ShikiruTask], task_id: str) -> Family | None:
    """Return the family of the task, or None when the task is not in the list."""
    by_id = {task.id: task for task in tasks}
    if task_id not in by_id:
        return None
    root = _root(by_id, by_id[task_id])
    members = tuple(task for task in tasks if _root(by_id, task).id == root.id)
    return Family(root, members)


def _root(by_id: dict[str, ShikiruTask], task: ShikiruTask) -> ShikiruTask:
    # keep: a parent cycle in Shikiru's list would otherwise never end the walk
    root, seen = task, {task.id}
    while root.parent_id is not None and root.parent_id in by_id and root.parent_id not in seen:
        root = by_id[root.parent_id]
        seen.add(root.id)
    return root
