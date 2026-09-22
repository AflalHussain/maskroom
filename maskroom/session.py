"""Session vaults: kept for import compatibility; the implementation lives in
maskroom.store.sessions (database-backed)."""
from .store.sessions import Session, SessionStore  # noqa: F401
