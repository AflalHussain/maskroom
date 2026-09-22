"""Request timings: engine level (the same files as the performance table in
docs/TECHNICAL_DESIGN.md) and HTTP API level against a running Postgres.

    docker compose up -d db
    pii_env/bin/python scripts/bench.py            # results printed + bench.json

Set MASKROOM_BENCH_OUT to choose the scratch directory (default: a temp dir)
and MASKROOM_BENCH_DB for the database URL."""
import json, os, statistics, subprocess, sys, tempfile, time, warnings
warnings.filterwarnings("ignore")
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = f"{ROOT}/tests/data"
S = os.environ.get("MASKROOM_BENCH_OUT") or tempfile.mkdtemp(prefix="maskroom-bench-")
DB_URL = os.environ.get("MASKROOM_BENCH_DB", "postgresql+psycopg://maskroom:maskroom@localhost:5432/maskroom")
sys.path.insert(0, ROOT)
os.environ.pop("MASKROOM_DATABASE_URL", None)

def engine_level():
    from maskroom import FinancialPrivacyEngine
    from maskroom.pipeline import mask_file
    rows = []
    for f in ["PII_Test_Dataset_LK.xlsx", "Real_World_Directory.xlsx", "PII_Stress_Test.xlsx",
              "SC_Judgment_Sample.pdf", "Ceylon_1888_Scan_Sample.pdf"]:
        eng = FinancialPrivacyEngine()
        t0 = time.time()
        mask_file(eng, os.path.join(DATA, f), os.path.join(S, "bench_" + f))
        rows.append((f"engine {f}", time.time() - t0))
    eng = FinancialPrivacyEngine()
    text = "Call Nimal Perera on 077-1234567, NIC 853421234V, email nimal@corp.lk. " * 20
    eng.pseudonymize_text(text)  # warm
    ts = []
    for _ in range(5):
        t0 = time.time(); eng.pseudonymize_text(text); ts.append(time.time() - t0)
    rows.append(("engine text (~1.5 KB, warm, median of 5)", statistics.median(ts)))
    return rows

def api_level():
    U = "http://127.0.0.1:5199"
    env = dict(os.environ, MASKROOM_DATABASE_URL=DB_URL,
               MASKROOM_DATA_DIR=f"{S}/benchdata", MASKROOM_ADMIN_KEY="adm", PII_TOKEN_SALT="bench", PORT="5199")
    p = subprocess.Popen([f"{ROOT}/pii_env/bin/python", f"{ROOT}/webui/app.py"], env=env,
                         stdout=open(f"{S}/bench_app.log", "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                requests.get(U + "/api/config", timeout=2); break
            except Exception:
                time.sleep(1)
        text = "Call Nimal Perera on 077-1234567, NIC 853421234V, email nimal@corp.lk. " * 20
        r = requests.post(U + "/api/mask", json={"text": text}).json()   # warm: model load
        sid = r["session_id"]
        rows = []
        for f in ["PII_Test_Dataset_LK.xlsx", "Real_World_Directory.xlsx", "PII_Stress_Test.xlsx",
                  "SC_Judgment_Sample.pdf", "Ceylon_1888_Scan_Sample.pdf"]:
            with open(os.path.join(DATA, f), "rb") as fh:
                t0 = time.time()
                r = requests.post(U + "/api/process", files={"file": (f, fh)},
                                  data={"session_id": sid, "preview": "false"}, timeout=600)
                dt = time.time() - t0
            r.raise_for_status()
            rows.append((f"api /api/process {f}", dt))
        def med(fn, n=5):
            ts = []
            for _ in range(n):
                t0 = time.time(); fn(); ts.append(time.time() - t0)
            return statistics.median(ts)
        masked = requests.post(U + "/api/mask", json={"text": text, "session_id": sid}).json()["masked"]
        rows.append(("api /api/mask text (~1.5 KB, session, median of 5)",
                     med(lambda: requests.post(U + "/api/mask", json={"text": text, "session_id": sid}))))
        rows.append(("api /api/unmask (median of 5)",
                     med(lambda: requests.post(U + "/api/unmask", json={"text": masked, "session_id": sid}))))
        n = requests.get(U + f"/api/session/{sid}").json()["vault_entries"]
        rows.append((f"api vault download ({n} entries, median of 5)",
                     med(lambda: requests.get(U + f"/api/session/{sid}/vault"))))
        rows.append(("api /api/session info (median of 5)",
                     med(lambda: requests.get(U + f"/api/session/{sid}"))))
        rows.append(("api /api/audit list, admin (median of 5)",
                     med(lambda: requests.get(U + "/api/audit", headers={"X-Admin-Key": "adm"}))))
        return rows
    finally:
        p.terminate(); p.wait()

out = engine_level() + api_level()
print("\n".join(f"{name:60} {t:8.2f} s" for name, t in out))
json.dump(out, open(f"{S}/bench.json", "w"))
print("written", f"{S}/bench.json")
