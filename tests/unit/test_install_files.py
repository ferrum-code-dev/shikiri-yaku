from __future__ import annotations

from pathlib import Path

from shikiri_yaku.adapters.install_files import InstanceInstallFiles


def test_output_of_an_install_run_is_a_file_in_the_output_directory(tmp_path: Path) -> None:
    files = InstanceInstallFiles(tmp_path)

    saved = Path(files.save_output(4, "fetching\ndone\n"))

    assert saved == tmp_path / "output" / "install-4.log"
    assert saved.read_text(encoding="utf-8") == "fetching\ndone\n"


def test_scratch_directory_is_created_inside_the_instance_directory(tmp_path: Path) -> None:
    scratch = InstanceInstallFiles(tmp_path).scratch_dir()

    assert scratch.is_dir()
    assert scratch.parent == tmp_path
