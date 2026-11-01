from __future__ import annotations

import stat
from pathlib import Path

from shikiri_yaku.adapters.install_files import InstanceInstallFiles
from shikiri_yaku.instance import OUTPUT_DIR_NAME


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_output_of_an_install_run_is_a_file_in_the_output_directory(tmp_path: Path) -> None:
    files = InstanceInstallFiles(tmp_path)

    saved = Path(files.save_output(4, "fetching\ndone\n"))

    assert saved == tmp_path / "output" / "install-4.log"
    assert saved.read_text(encoding="utf-8") == "fetching\ndone\n"


def test_output_directory_and_file_are_readable_by_their_owner_only(tmp_path: Path) -> None:
    saved = Path(InstanceInstallFiles(tmp_path).save_output(4, "token printed\n"))

    assert _mode(saved.parent) == 0o700
    assert _mode(saved) == 0o600


def test_an_output_written_with_wider_modes_is_narrowed_when_it_is_written_again(
    tmp_path: Path,
) -> None:
    folder = tmp_path / OUTPUT_DIR_NAME
    folder.mkdir()
    folder.chmod(0o755)
    (folder / "install-4.log").write_text("old\n", encoding="utf-8")
    (folder / "install-4.log").chmod(0o644)

    saved = Path(InstanceInstallFiles(tmp_path).save_output(4, "new\n"))

    assert _mode(folder) == 0o700
    assert _mode(saved) == 0o600
    assert saved.read_text(encoding="utf-8") == "new\n"


def test_scratch_directory_is_created_inside_the_instance_directory(tmp_path: Path) -> None:
    scratch = InstanceInstallFiles(tmp_path).scratch_dir()

    assert scratch.is_dir()
    assert scratch.parent == tmp_path
