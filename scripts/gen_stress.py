"""Hostile synthetic workbook + machine-readable answer key. All data fake."""
import random, json, openpyxl
random.seed(7)
import os
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "tests", "data", "PII_Stress_Test.xlsx")
KEY = os.path.join(HERE, "..", "tests", "data", "PII_Stress_Test_key.json")

first = ["Nimal","Kumari","Thanuja","Sanduni","Ashen","Dilani","Ruwan","Fathima","Suresh","Chathura","Ishara","Mohamed"]
last  = ["Perera","Wickramasinghe","Sivakumar","Jayawardena","Fernando","Bandara","Rizwan","Thirunavukkarasu","Gunasekara","Silva","Herath","Abeysekera"]
cities = ["Colombo 03","Kandy","Galle","Jaffna","Negombo","Matara","Kurunegala","Batticaloa","Dehiwala","Ratnapura","Anuradhapura","Badulla"]
def name(i): return f"{first[i%12]} {last[(i*5)%12]}"
def caps_name(i): return f"{last[(i*5)%12].upper()}, {first[i%12].upper()} {chr(65+i%26)}"
def nic_old(i): return f"{70+i%25:02d}{(i*37)%366+1:03d}{1000+i*73:04d}V"
def nic_new(i): return f"{1970+i%40}{(i*53)%366+1:03d}{10000+i*311:05d}"
def mobile(i): return f"07{[1,2,5,6,7,8][i%6]}-{100+i*7:03d}{4000+i*13:04d}"
def landline(i): return f"0{[11,21,81,91,31,41][i%6]}-{2000000+i*1234:07d}"
def email(i): return f"{first[i%12].lower()}.{last[(i*5)%12].lower()[:6]}@{['lankamail.lk','ceylonet.lk','corp.example','mail.example.org'][i%4]}"
def luhn_ok(digs):
    t=0
    for k,d in enumerate(reversed(digs)):
        d=int(d)
        if k%2==1: d=d*2; d = d-9 if d>9 else d
        t+=d
    return t%10==0
def card(i, valid=True):
    base = f"4{random.randint(10**13,10**14-1)}"  # 15 digits -> add check digit
    for c in range(10):
        cand = base+str(c)
        if luhn_ok(cand) == valid:
            break
    return " ".join(cand[j:j+4] for j in range(0,16,4))
def address(i): return f"No. {10+i*3}, {['Galle Road','Temple Road','Main Street','Marine Drive','Lake Road','Hill Street'][i%6]}, {cities[i%12]}"
N = 12
key = {}   # (sheet, col_letter) -> "MASK" | "KEEP"
wb = openpyxl.Workbook()

# ---- Sheet 1: cryptic headers ----
ws = wb.active; ws.title = "Cryptic Headers"
ws.append(["C1","C2","C3","C4","C5","C6","C7","C8","C9"])
for i in range(N):
    ws.append([nic_old(i) if i!=5 else "N/A", email(i), mobile(i), landline(i),
               int(nic_new(i)),                  # new NIC stored as NUMBER
               card(i, True),                    # valid Luhn cards
               f"{3000000000+i*9973}",           # 10-digit order ids (decoy)
               15000 + i*1250,                   # amounts
               card(i, False)])                  # INVALID Luhn (decoy)
for c,exp in zip("ABCDEFGHI", ["MASK","MASK","MASK","MASK","MASK","MASK","KEEP","KEEP","KEEP"]):
    key[f"Cryptic Headers|{c}"] = exp

# ---- Sheet 2: Sinhala headers ----
ws = wb.create_sheet("Sinhala Headers")
ws.append(["සේවක අංකය","නම","ජා.හැ. අංකය","දුරකථන","ලිපිනය","වැටුප (රු.)"])
for i in range(N):
    ws.append([f"EMP-{100+i}", name(i), nic_old(i) if i%2 else nic_new(i), mobile(i), address(i), 85000+i*5000])
for c,exp in zip("ABCDEF", ["KEEP","MASK","MASK","MASK","MASK","KEEP"]):
    key[f"Sinhala Headers|{c}"] = exp

# ---- Sheet 3: header buried at row 16 ----
ws = wb.create_sheet("Deep Header")
ws.append(["Quarterly customer export — internal"])
for r in range(13):
    ws.append([f"Note {r+1}: generated for regression testing of the export pipeline."])
ws.append([])
ws.append(["Customer Name","National ID","Email","Contact","Outstanding (LKR)","Last Payment"])
for i in range(N):
    ws.append([caps_name(i), nic_new(i), email(i), landline(i), 1250.5*(i+1), f"2026-0{1+i%9}-{10+i}"])
for c,exp in zip("ABCDEF", ["MASK","MASK","MASK","MASK","KEEP","KEEP"]):
    key[f"Deep Header|{c}"] = exp

# ---- Sheet 4: stacked tables ----
ws = wb.create_sheet("Stacked Tables")
ws.append(["Name","Mobile","Plan"])
for i in range(6):
    ws.append([name(i), mobile(i), ["Basic","Pro","Max"][i%3]])
ws.append([]); ws.append([])
ws.append(["Ref","Contact","Passport","Notes"])    # second table, header row 10
for i in range(N):
    ws.append([f"R-{i:03d}", mobile(i+3), f"N{4500000+i*137}", "follow up" if i%2 else "closed"])
key["Stacked Tables|A"] = "MIXED"   # names rows 2-7 MASK; refs rows 11+ KEEP
key["Stacked Tables|B"] = "MASK"
key["Stacked Tables|C"] = "MIXED"   # plan KEEP (top), passport MASK (bottom)
key["Stacked Tables|D"] = "KEEP"

# ---- Sheet 5: decoys ----
ws = wb.create_sheet("Decoys")
ws.append(["Vehicle","Invoice","Barcode (UPC)","Product Code","Year","Ref Code"])
for i in range(N):
    ws.append([f"WP CA{chr(65+i%26)}-{1000+i*37}", f"INV-2026-{1000+i:05d}",
               f"0{random.randint(10**10,10**11-1)}",          # 12-digit starting with 0
               f"PRD-{random.randint(10**8,10**9-1)}",          # 9 digits with prefix
               1995+i, (f"{random.randint(100,999)}-{random.randint(10,99)}-{random.randint(1000,9999)}" if i%2 else f"RC/{random.randint(1000,9999)}")])  # only HALF SSN-shaped -> below profile ratio
for c in "ABCDEF":
    key[f"Decoys|{c}"] = "KEEP"

# ---- Sheet 6: free text ----
ws = wb.create_sheet("Free Text")
ws.append(["Ticket","Notes"])
notes = [
  ("T1", f"Tel: {landline(1)} — caller {name(1)} asked for balance. Order {3000000000+77} shipped."),
  ("T2", f"Sent OTP to {mobile(2)}; NIC on file {nic_old(2)}. Refund Rs. 12,450.00 approved."),
  ("T3", f"{caps_name(3)} emailed from {email(3)} about invoice INV-2026-01003."),
  ("T4", f"Card {card(4,True)} declined twice. Hotline 1919 informed customer."),
  ("T5", f"New NIC {nic_new(5)} verified against passport N{4500000+5*137}. Vehicle WP CAB-1234."),
  ("T6", f"Meeting on 25 September 2026 at Colombo office. Salary revision to 285000 noted."),
]
for t in notes: ws.append(list(t))
key["Free Text|A"] = "KEEP"
must_mask = [landline(1), name(1), mobile(2), nic_old(2), caps_name(3).split(",")[0], email(3), card(4,True), nic_new(5), f"N{4500000+5*137}"]
must_keep = [f"{3000000000+77}", "12,450.00", "INV-2026-01003", "1919", "WP CAB-1234", "25 September 2026", "285000"]

# ---- Answer key sheet (human readable) ----
ws = wb.create_sheet("Answer Key")
ws.append(["Sheet","Column","Expected"])
for k,v in key.items():
    sh, col = k.split("|"); ws.append([sh, col, v])
wb.save(OUT)
json.dump({"columns": key, "free_text_must_mask": must_mask, "free_text_must_keep": must_keep}, open(KEY,"w"), indent=1)
print("written", OUT)
