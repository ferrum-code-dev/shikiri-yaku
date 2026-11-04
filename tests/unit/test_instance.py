"""Unit tests for the instance directory and its lock."""

from __future__ import annotations

import multiprocessing
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.instance import InstanceLockedError, instance_dir, instance_lock

if TYPE_CHECKING:
    from multiprocessing.synchronize import Event
    from pathlib import Path


def _hold_lock(directory: Path, held: Event, released: Event) -> None:
    with instance_lock(directory):
        held.set()
        released.wait()


def test_the_instance_directory_is_named_after_the_product_under_home(tmp_path: Path) -> None:
    assert instance_dir("shop", tmp_path) == tmp_path / "shop"


def test_a_second_process_is_refused_while_the_holder_runs_and_admitted_after_it_exits(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    held = context.Event()
    released = context.Event()
    holder = context.Process(target=_hold_lock, args=(tmp_path, held, released))
    holder.start()
    held.wait()
    try:
        with pytest.raises(InstanceLockedError), instance_lock(tmp_path):
            pass
    finally:
        released.set()
        holder.join()

    assert holder.exitcode == 0
    with instance_lock(tmp_path):
        pass
