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
import ast
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


# ------------------------------------------------------------ attributes in the wrong class
def _init_assigns(cls: ast.ClassDef) -> set:
    """Everything the class settles before any method runs: class-level names,
    method names, and what __init__ assigns to self."""
    out = set()
    for node in cls.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in ([target] if isinstance(target, ast.Name)
                             else getattr(target, "elts", [])):
                    if isinstance(name, ast.Name):
                        out.add(name.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
            if node.name == "__init__":
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name)
                            and inner.value.id == "self" and isinstance(inner.ctx, ast.Store)):
                        out.add(inner.attr)
    return out


def test_no_class_reads_an_attribute_another_class_initialises():
    """The signature of a line pasted into the wrong class.

    `update_checked` was initialised in OverlayWorker's __init__ and read in
    Automation's update poll. Automation does assign it -- on the line after the
    one that reads it -- so nothing looked wrong, and the helper threw an
    AttributeError on every idle tick for weeks. It was caught by the log filling
    up on a user's machine, not here: the update check had never once run.

    Narrow on purpose. An attribute set outside __init__ is ordinary (the bar's
    panel is made when it opens); an attribute set in *another class's* __init__
    and read here is somebody's copy and paste.
    """
    offenders = []
    for path in sources():
        tree = ast.parse(path.read_text("utf-8"))
        classes = {c.name: c for c in ast.walk(tree) if isinstance(c, ast.ClassDef)}
        settled = {name: _init_assigns(c) for name, c in classes.items()}
        for name, cls in classes.items():
            seen = {}
            for node in ast.walk(cls):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == "self" and isinstance(node.ctx, ast.Load)):
                    seen.setdefault(node.attr, node.lineno)
            for attr, line in seen.items():
                if attr in settled[name]:
                    continue
                owners = [o for o, s in settled.items() if o != name and attr in s]
                if owners:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{line} {name} reads self.{attr}, "
                        f"which only {', '.join(owners)} sets in __init__")
    assert not offenders, "\n  " + "\n  ".join(offenders)
