from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest  # type: ignore  # no pytest in typeshed
from py._path.local import LocalPath  # type: ignore  # no py in typeshed

from mypy.build import BuildSource
from mypy.find_sources import create_source_list
from mypy.options import Options

MergeFinder = Callable[
    [Dict[str, Any], bool, Optional[Sequence[str]]], Tuple[Path, List[BuildSource]]
]


def create_files(folder: Path, content: Dict[str, Any]) -> None:
    if not folder.exists():
        folder.mkdir()
    for key, value in content.items():
        if key.endswith(".py") or key.endswith(".pyi"):
            with open(str(folder / key), "wt") as file_handler:
                file_handler.write(value)
        elif isinstance(value, dict):
            create_files(folder / key, value)


@pytest.fixture()
def merge_finder(  # type: ignore  # cannot follow this import
    tmpdir: LocalPath, monkeypatch: Any
) -> MergeFinder:
    def _checker(
        content: Dict[str, Any], merge: bool, args: Optional[Sequence[str]] = None
    ) -> Tuple[Path, List[BuildSource]]:
        monkeypatch.chdir(tmpdir)
        options = Options()
        options.merge_stub_into_src = merge
        test_dir = str(tmpdir)
        create_files(Path(test_dir), content)
        targets = create_source_list(files=[test_dir] if args is None else args, options=options)
        return Path(str(tmpdir)), targets

    return _checker


def test_source_finder_merge(merge_finder: MergeFinder) -> None:
    base_dir, found = merge_finder({"a.py": "", "a.pyi": ""}, True, None)
    assert found == [
        BuildSource(
            path=str(base_dir / "a.py"),
            base_dir=str(base_dir),
            module="a",
            text=None,
            merge_with=BuildSource(
                path=str(base_dir / "a.pyi"), base_dir=str(base_dir), module="a", text=None
            ),
        )
    ]


def test_source_finder_merge_sub_folder(merge_finder: MergeFinder) -> None:
    base_dir, found = merge_finder(
        {"pkg": {"a.py": "", "a.pyi": "", "__init__.py": ""}}, True, None
    )
    assert found == [
        BuildSource(
            path=str(base_dir / "pkg" / "__init__.py"),
            base_dir=str(base_dir),
            module="pkg",
            text=None,
        ),
        BuildSource(
            path=str(base_dir / "pkg" / "a.py"),
            base_dir=str(base_dir),
            module="pkg.a",
            text=None,
            merge_with=BuildSource(
                path=str(base_dir / "pkg" / "a.pyi"),
                base_dir=str(base_dir),
                module="pkg.a",
                text=None,
            ),
        ),
    ]


def test_source_finder_no_merge(merge_finder: MergeFinder) -> None:
    base_dir, found = merge_finder({"a.py": "", "a.pyi": ""}, False, None)
    assert found == [
        BuildSource(path=str(base_dir / "a.pyi"), base_dir=str(base_dir), module="a", text=None)
    ]


@pytest.mark.parametrize("merge", [True, False])
def test_source_finder_merge_just_source(merge_finder: MergeFinder, merge: bool) -> None:
    base_dir, found = merge_finder({"a.py": ""}, merge, None)
    assert found == [
        BuildSource(path=str(base_dir / "a.py"), base_dir=str(base_dir), module="a", text=None)
    ]


@pytest.mark.parametrize("merge", [True, False])
def test_source_finder_merge_just_stub(merge_finder: MergeFinder, merge: bool) -> None:
    base_dir, found = merge_finder({"a.pyi": ""}, merge, None)
    assert found == [
        BuildSource(path=str(base_dir / "a.pyi"), base_dir=str(base_dir), module="a", text=None)
    ]


def test_source_finder_matching_exists(merge_finder: MergeFinder) -> None:
    base_dir, found = merge_finder({"a.py": "", "a.pyi": ""}, True, ["a.py"])
    assert found == [
        BuildSource(
            path="a.py",
            base_dir=".",
            module="a",
            text=None,
            merge_with=BuildSource(path="a.pyi", base_dir=".", module="a", text=None),
        )
    ]


def test_source_finder_matching_exists_stub_specified(merge_finder: MergeFinder) -> None:
    base_dir, found = merge_finder({"a.py": "", "a.pyi": ""}, True, ["a.py", "a.pyi"])
    assert found == [
        BuildSource(
            path="a.py",
            base_dir=".",
            module="a",
            text=None,
            merge_with=BuildSource(path="a.pyi", base_dir=".", module="a", text=None),
        )
    ]


def test_source_finder_matching_exists_stub_specified_first(merge_finder: MergeFinder) -> None:
    base_dir, found = merge_finder({"a.py": "", "a.pyi": ""}, True, ["a.pyi", "a.py"])
    assert found == [
        BuildSource(
            path="a.py",
            base_dir=".",
            module="a",
            text=None,
            merge_with=BuildSource(path="a.pyi", base_dir=".", module="a", text=None),
        )
    ]
