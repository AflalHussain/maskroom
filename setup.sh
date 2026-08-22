#!/usr/bin/env bash
# One-shot setup for the PII masking engine.
#   ./setup.sh            -> core engine + web UI (en_core_web_lg)
#   ./setup.sh --trf      -> also install the transformer NER model (CPU torch)
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
echo "==> Checking prerequisites"
$PY -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"; print("python", sys.version.split()[0])'
if ! command -v tesseract >/dev/null; then
  echo "!!  tesseract not found. Scanned PDFs will NOT be redacted until it is installed:"
  echo "    sudo apt install tesseract-ocr        (Debian/Ubuntu)"
else
  echo "tesseract $(tesseract --version 2>&1 | head -1 | awk '{print $2}')"
fi

echo "==> Creating virtualenv pii_env"
[ -d pii_env ] || $PY -m venv pii_env
./pii_env/bin/pip install --quiet --upgrade pip
./pii_env/bin/pip install --quiet -r requirements.txt

echo "==> Downloading spaCy model en_core_web_lg (~560 MB, once)"
./pii_env/bin/python -m spacy download en_core_web_lg --quiet

if [ "${1:-}" = "--trf" ]; then
  echo "==> Installing transformer support (CPU-only torch, ~200 MB) and en_core_web_trf (~460 MB)"
  ./pii_env/bin/pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
  ./pii_env/bin/pip install --quiet -r requirements-trf.txt
  ./pii_env/bin/python -m spacy download en_core_web_trf --quiet
fi

echo "==> Verifying"
./pii_env/bin/python - <<'PY'
import warnings; warnings.filterwarnings("ignore")
from masking import FinancialPrivacyEngine
e = FinancialPrivacyEngine()
masked, _ = e.pseudonymize_text("Call Nimal Perera on phone 077-1234567, NIC 853421234V")
assert "TOK_" in masked, masked
print("engine OK ->", masked)
PY

echo
echo "Setup complete. Next:"
echo "  export PII_TOKEN_SALT='choose-a-secret'        # token salt (keep it secret)"
echo "  pii_env/bin/python masking.py file.xlsx out.xlsx --vault vault.json"
echo "  pii_env/bin/python webui/app.py                 # web UI at http://127.0.0.1:5170"
