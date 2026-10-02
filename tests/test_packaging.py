"""The PyInstaller spec and the code it freezes have to agree.

Both faults this catches were in the spec for weeks and could only have surfaced
by running the build on Windows, which nobody had:

- `http.server` and `email` were on the exclude list while the sign-in listener
  and the file broker are both built on them. Excluding a module the program
  imports does not make the build smaller, it makes it fail at first use.
- `broker` was neither on `pathex` nor in `hiddenimports`, so a frozen helper
  would have come up with no folder sharing and no stdio bridge, and said
  nothing about why.

Read, not executed: the spec is valid Python but refers to names PyInstaller
injects, so it is parsed instead.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "desktop/packaging/safepii-helper.spec"
FROZEN = ("desktop/helper.py", "desktop/broker.py")


def analysis_args() -> dict:
    """The keyword arguments of the Analysis(...) call, as literals."""
    tree = ast.parse(SPEC.read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis":
            out = {}
            for kw in node.keywords:
                try:
                    out[kw.arg] = ast.literal_eval(kw.value)
                except ValueError:
                    out[kw.arg] = None          # computed, e.g. pathex
            return out
    raise AssertionError("no Analysis(...) call in the spec")


def imported_top_levels() -> set[str]:
    names = set()
    for rel in FROZEN:
        tree = ast.parse((ROOT / rel).read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module.split(".")[0])
    return names


@pytest.fixture(scope="module")
def spec():
    if not SPEC.exists():
        pytest.skip("no packaging spec in this checkout")
    return analysis_args()


def test_nothing_the_helper_imports_is_excluded(spec):
    """The failure this prevents is a build that starts and then dies at the
    first sign-in, which is the worst moment to find out."""
    excluded = {e.split(".")[0] for e in (spec.get("excludes") or [])}
    clash = sorted(excluded & imported_top_levels())
    assert not clash, f"excluded but imported: {clash}"


def test_the_broker_is_named_so_it_cannot_be_left_out(spec):
    """helper.py imports it inside a try/except, after putting its own folder on
    sys.path at runtime -- neither of which a static analysis follows."""
    assert "broker" in (spec.get("hiddenimports") or [])


def test_the_broker_s_folder_is_on_the_analysis_path():
    text = SPEC.read_text("utf-8")
    assert 'pathex=[HERE, os.path.join(HERE, "desktop")]' in text


def test_the_exclude_list_still_drops_the_test_suite(spec):
    """It is there to keep the build small; the point is not to stop excluding."""
    assert "pytest" in (spec.get("excludes") or [])
