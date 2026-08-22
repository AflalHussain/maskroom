"""Maskroom — PII pseudonymization for spreadsheets and redaction for PDFs.

Public API:
    from maskroom import FinancialPrivacyEngine
"""
from .engine import FinancialPrivacyEngine

__all__ = ["FinancialPrivacyEngine"]
__version__ = "0.2.0"
