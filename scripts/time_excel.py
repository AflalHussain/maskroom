"""Time the Excel pipeline on the corpus.  Usage: python scripts/time_excel.py [en_core_web_trf]"""
import os, sys, time, warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from maskroom import FinancialPrivacyEngine

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests", "data")
model = sys.argv[1] if len(sys.argv) > 1 else None
for f in ["PII_Test_Dataset_LK.xlsx", "Real_World_Directory.xlsx", "PII_Stress_Test.xlsx"]:
    eng = FinancialPrivacyEngine(nlp_model=model)
    t0 = time.time()
    eng.pseudonymize_excel(os.path.join(DATA, f), os.path.join(os.environ.get("TMPDIR", "/tmp"), f"_timing_{f}"))
    print(f"{model or 'lg':16} {f:32} {time.time()-t0:6.2f}s  analyzer_calls={eng.analyzer_calls}  findings={len(eng.report)}")
