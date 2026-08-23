"""Install built artifacts into separate clean environments and smoke public APIs."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


SMOKE = r'''
import importlib.metadata as metadata
import json
import tempfile
from importlib.resources import files
from pathlib import Path

import chinese_exam_kit
from chinese_exam_kit.content.validate import validate_content_dir
from chinese_exam_kit.knowledge import build_index, load_contract, search_cards, validate_library

package_path = Path(chinese_exam_kit.__file__).resolve()
assert "site-packages" in package_path.parts or "dist-packages" in package_path.parts
content_contract = json.loads(files("chinese_exam_kit.resources").joinpath("content_contract.json").read_text(encoding="utf-8"))
knowledge_contract = load_contract()
assert content_contract["schema_version"] == 1
assert knowledge_contract["schema_version"] == 1
with tempfile.TemporaryDirectory(prefix="cekit-installed-smoke-") as temporary:
    root = Path(temporary) / "knowledge"
    library = validate_library(root, knowledge_contract)
    assert library.errors == () and library.cards == ()
    assert build_index((), knowledge_contract, root)["cards"] == []
    assert search_cards(root, "") == ()
distribution = metadata.distribution("chinese-exam-teaching-kit")
assert distribution.metadata["License-Expression"] == "Apache-2.0"
license_files = set(distribution.metadata.get_all("License-File") or ())
assert {"LICENSE", "NOTICE"} <= license_files
'''


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def smoke(artifact: Path) -> None:
    source = artifact.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="cekit-noneditable-") as temporary:
        root = Path(temporary)
        subprocess.run([sys.executable, "-m", "venv", str(root / "venv")], check=True)
        python = _venv_python(root / "venv")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(source)],
            cwd=root,
            check=True,
        )
        subprocess.run([str(python), "-I", "-c", SMOKE], cwd=root, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts", nargs="+")
    args = parser.parse_args()
    for value in args.artifacts:
        smoke(Path(value))
    print(f"non-editable package smoke passed for {len(args.artifacts)} artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
