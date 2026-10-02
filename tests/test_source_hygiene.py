"""Every source file compiles without a warning.

One escaped its way onto a user's machine: a docstring describing a Windows path
as `Packages\\...\\LocalCache` in an ordinary string literal, where `\\.` is an
invalid escape sequence. Python still ran it, with a SyntaxWarning printed above
the first line of output — which is a poor way for somebody testing a build to be
greeted, and the sort of thing that is invisible on the machine it was written on
if that machine never runs the file.

Cheap to check and it covers the whole repository, so it also catches the next
one.
"""
import pathlib
import py_compile
import warnings

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WHERE = ("desktop", "maskroom", "webui", "scripts", "tests", "samples")


def sources():
    for top in WHERE:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in path.parts or "pii_env" in path.parts:
                continue
            yield path


def test_there_is_something_to_check():
    assert len(list(sources())) > 20


@pytest.mark.parametrize("path", list(sources()), ids=lambda p: str(p.relative_to(ROOT)))
def test_a_source_file_compiles_without_warnings(path, tmp_path):
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        try:
            py_compile.compile(str(path), cfile=str(tmp_path / "out.pyc"), doraise=True)
        except py_compile.PyCompileError as e:
            pytest.fail(f"{path.relative_to(ROOT)} does not compile: {e}")
    said = [f"line {w.lineno}: {w.category.__name__}: {w.message}" for w in raised]
    assert not said, f"{path.relative_to(ROOT)}\n  " + "\n  ".join(said)
