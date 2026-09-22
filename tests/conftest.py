import os
import tempfile
import warnings

import pytest

warnings.filterwarnings("ignore")
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Runtime state goes to a private in-memory SQLite database and a throwaway
# data dir, set before any test module imports webui.app (which opens the
# database at import). MASKROOM_TEST_DATABASE_URL runs the same suite against
# a real server, e.g. the Postgres container from docker-compose.yml.
os.environ["MASKROOM_DATABASE_URL"] = os.environ.get("MASKROOM_TEST_DATABASE_URL", "sqlite://")
os.environ["MASKROOM_DATA_DIR"] = tempfile.mkdtemp(prefix="maskroom-test-")


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


def _fresh_db():
    """A private engine with an empty schema. Each in-memory SQLite engine is
    its own database; on a server URL the schema is dropped and recreated."""
    from maskroom.store import db as db_mod, schema
    engine = db_mod.make_engine(os.environ["MASKROOM_DATABASE_URL"])
    schema.metadata.drop_all(engine)
    db_mod.init_schema(engine)
    return engine


@pytest.fixture
def db():
    engine = _fresh_db()
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def db_module():
    engine = _fresh_db()
    yield engine
    engine.dispose()
