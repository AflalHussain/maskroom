import os
import warnings

import pytest

warnings.filterwarnings("ignore")
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def pytest_addoption(parser):
    parser.addoption("--nlp-model", default=None,
                     help="spaCy model to test with (default: en_core_web_lg)")


@pytest.fixture(scope="session")
def nlp_model(request):
    return request.config.getoption("--nlp-model")


@pytest.fixture(scope="session")
def make_engine(nlp_model):
    """Factory: a fresh engine (empty vault/report) with the session model."""
    from maskroom import FinancialPrivacyEngine

    def _make(**kw):
        kw.setdefault("nlp_model", nlp_model)
        kw.setdefault("salt", "test-salt")
        return FinancialPrivacyEngine(**kw)
    return _make


@pytest.fixture(scope="session")
def engine(make_engine):
    """Shared engine for read-only text tests (model load is slow)."""
    return make_engine()


@pytest.fixture
def data_path():
    return lambda name: os.path.join(DATA, name)
