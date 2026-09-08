"""Maskroom — PII pseudonymization for spreadsheets and redaction for PDFs.

Public API:
    from maskroom import FinancialPrivacyEngine
"""
from .engine import FinancialPrivacyEngine, build_nlp_engine
from .session import SessionStore

__all__ = ["FinancialPrivacyEngine", "SessionStore", "build_nlp_engine"]
__version__ = "0.2.0"
