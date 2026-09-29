"""The Group Policy template and the helper have to agree.

Two ways this breaks quietly. A `$(string.X)` with no matching string in the
.adml makes the Group Policy editor refuse the whole template, which an
administrator sees and we would not. A `valueName` the helper does not accept is
worse: the setting appears in the editor, an administrator sets it, and nothing
happens — the helper logs "ignoring unknown setting" on a machine nobody reads
the log of.
"""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ADMX = ROOT / "enterprise/policies/windows/admx/SafePII.admx"
ADML = ROOT / "enterprise/policies/windows/admx/en-US/SafePII.adml"


@pytest.fixture(scope="module")
def template():
    if not ADMX.exists():
        pytest.skip("no Group Policy template in this checkout")
    return ADMX.read_text("utf-8"), ADML.read_text("utf-8")


def test_both_files_are_valid_xml(template):
    for path in (ADMX, ADML):
        ET.parse(path)


def test_every_referenced_string_and_presentation_exists(template):
    admx, adml = template
    strings = set(re.findall(r'<string id="([^"]+)"', adml))
    shown = set(re.findall(r'<presentation id="([^"]+)"', adml))
    assert not set(re.findall(r"\$\(string\.([^)]+)\)", admx)) - strings
    assert not set(re.findall(r"\$\(presentation\.([^)]+)\)", admx)) - shown


def test_nothing_is_defined_and_never_used(template):
    """A leftover string is a setting somebody removed and half-tidied."""
    admx, adml = template
    strings = set(re.findall(r'<string id="([^"]+)"', adml))
    assert not strings - set(re.findall(r"\$\(string\.([^)]+)\)", admx))


def test_every_setting_is_one_the_helper_accepts(template):
    """Otherwise an administrator sets something that does nothing."""
    admx, _adml = template
    src = (ROOT / "desktop/helper.py").read_text("utf-8")
    block = src[src.index("POLICY_TYPES = {"):src.index("POLICY: dict")]
    accepted = set(re.findall(r'"([A-Za-z]+)": (?:str|bool|int)', block))
    assert accepted, "could not read POLICY_TYPES"
    offered = set(re.findall(r'valueName="([^"]+)"', admx))
    assert offered, "the template offers no settings at all"
    assert not offered - accepted, f"not accepted by the helper: {sorted(offered - accepted)}"


def test_the_registry_key_is_the_one_the_helper_reads(template):
    admx, _adml = template
    src = (ROOT / "desktop/helper.py").read_text("utf-8")
    key = re.search(r'POLICY_KEY = r"([^"]+)"', src).group(1)
    assert f'key="{key}"' in admx
    assert admx.count(f'key="{key}"') == admx.count("<policy name=")
