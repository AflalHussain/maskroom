"""Regenerate the demo/test sample files in this directory.

    pii_env/bin/python samples/generate_samples.py

Every person, identifier, address and card number here is invented. Card
numbers are the public Luhn-valid test numbers; NICs follow the Sri Lankan
formats but belong to nobody.
"""
import os

import openpyxl
from openpyxl.styles import Font

try:
    import pymupdf as fitz
except ImportError:
    import fitz

HERE = os.path.dirname(os.path.abspath(__file__))


def workbook(path, sheets):
    """sheets: [(title, header, rows, title_rows_above_header)]"""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, header, rows, above in sheets:
        ws = wb.create_sheet(title)
        for line in above:
            ws.append([line])
        if above:
            ws.append([])
        ws.append(header)
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append(r)
        for col_cells in ws.columns:
            width = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
            ws.column_dimensions[col_cells[0].column_letter].width = min(max(10, width + 2), 48)
    wb.save(os.path.join(HERE, path))
    print("wrote", path)


# --------------------------------------------------------------- set 1: loans
def loans():
    header = ["Account No", "Customer Name", "NIC", "Mobile", "Email", "Branch", "Product",
              "Disbursed", "Balance (LKR)", "Days Overdue", "Last Payment", "Collections Note"]
    rows = [
        ["ABC-1234-002", "Nimal Perera", "853421234V", "077-1234567", "nimal.perera@gmail.com", "Kandy", "Personal loan", "2024-02-14", 182500, 45, "2026-07-20", "Promised to pay by 15 Sep. Wife Malini Perera answered on 071-5551234."],
        ["ABC-1234-017", "Kumari Bandara", "199085601234", "071-9876543", "kumari.b@lankamail.lk", "Kandy", "Vehicle lease", "2025-05-02", 64000, 12, "2026-08-22", "First missed instalment."],
        ["ABC-5678-003", "Ruwan Jayawardena", "912345678V", "+94 76 555 1234", "ruwan.j@hotmail.com", "Negombo", "Personal loan", "2023-09-30", 410000, 90, "2026-06-05", "Not reachable. Field visit to 88 Temple Lane, Negombo on 2 Sep — premises closed."],
        ["ABC-5678-021", "Sivakumar Rajendran", "780987654V", "075-3334445", "siva.r@yahoo.com", "Jaffna", "Business loan", "2022-11-11", 1250000, 120, "2026-05-01", "Guarantor Thavam Rajendran (NIC 550123456V) contacted on 021-2223344."],
        ["ABC-9012-008", "Chathurika Herath", "199678900123", "070-1112223", "chathurika.h@gmail.com", "Kurunegala", "Housing loan", "2021-06-18", 6890000, 30, "2026-08-01", "Salary delayed, employer Ceylon Tea Exports."],
        ["ABC-9012-014", "Mohamed Fazil", "861234567V", "077-9990001", "fazil.m@outlook.com", "Colombo 06", "Credit card", "2025-01-09", 145600, 60, "2026-07-10", "Card 5555 5555 5555 4444 blocked. Requested settlement plan."],
    ]
    policy = [
        ["Days overdue", "Action"],
        ["1–30", "SMS reminder"],
        ["31–60", "Call + written notice"],
        ["61–90", "Final notice, restructure offer"],
        ["> 90", "Legal review"],
    ]
    workbook("loans_overdue_register.xlsx", [
        ("Overdue Sep 2026", header, rows, ["Sunrise Finance PLC — overdue accounts as at 9 September 2026"]),
        ("Collections Policy", policy[0], policy[1:], []),
    ])


# ------------------------------------------------------------ set 2: HR leave
def hr():
    header = ["Employee ID", "Employee Name", "NIC", "Mobile", "Email", "Designation", "Date", "Leave Type", "Certified?", "Days", "Remarks"]
    people = {
        "EMP-1042": ("Chaminda Silva", "871234567V", "077-2345678", "chaminda.silva@lankafin.lk", "Senior Accountant"),
        "EMP-1057": ("Thilini Fernando", "199234500789", "071-3456789", "thilini.f@lankafin.lk", "Accounts Executive"),
        "EMP-1063": ("Mohamed Rizwan", "901122334V", "076-4567890", "m.rizwan@lankafin.lk", "Payroll Officer"),
        "EMP-1071": ("Dilshan Rajapaksa", "199512345678", "075-5678901", "dilshan.r@lankafin.lk", "Junior Accountant"),
        "EMP-1088": ("Nadeesha Kumari", "199745601122", "070-6789012", "nadeesha.k@lankafin.lk", "Accounts Assistant"),
    }
    leave = [
        ("EMP-1042", "2026-08-12", "Sick", "No", 1, "Called in at 8.40am"),
        ("EMP-1042", "2026-08-13", "Sick", "No", 1, ""),
        ("EMP-1042", "2026-08-14", "Sick", "No", 1, "No medical certificate submitted"),
        ("EMP-1042", "2026-08-20", "Casual", "n/a", 1, "Approved by Mrs. Sanduni Wickramasinghe"),
        ("EMP-1042", "2026-08-25", "Annual", "n/a", 3, "25–27 Aug, pre-approved"),
        ("EMP-1057", "2026-08-04", "Annual", "n/a", 5, "4–8 Aug, pre-approved"),
        ("EMP-1063", "2026-08-06", "Sick", "No", 1, ""),
        ("EMP-1063", "2026-08-07", "Sick", "No", 1, ""),
        ("EMP-1063", "2026-08-19", "Sick", "No", 1, "Informed by SMS only"),
        ("EMP-1063", "2026-08-21", "Sick", "No", 1, ""),
        ("EMP-1063", "2026-08-28", "Casual", "n/a", 1, ""),
        ("EMP-1071", "2026-08-01", "Casual", "n/a", 1, ""),
        ("EMP-1071", "2026-08-11", "Half-day absence", "No", 0.5, "Left at 1pm, not informed"),
        ("EMP-1071", "2026-08-18", "Half-day absence", "No", 0.5, ""),
        ("EMP-1071", "2026-08-25", "Half-day absence", "No", 0.5, ""),
        ("EMP-1088", "2026-08-15", "Sick", "Yes", 2, "15–16 Aug, medical certificate on file"),
    ]
    rows = [[eid, *people[eid], d, t, c, n, r] for eid, d, t, c, n, r in leave]
    master_h = ["Employee ID", "Employee Name", "NIC", "Date of Birth", "Mobile", "Home Address", "City", "Designation", "Basic Salary (LKR)", "Emergency Contact", "Emergency Phone"]
    master = [
        ["EMP-1042", "Chaminda Silva", "871234567V", "1987-05-03", "077-2345678", "No. 12, Templers Road, Mount Lavinia", "Mount Lavinia", "Senior Accountant", 185000, "Ruwani Silva", "077-8765432"],
        ["EMP-1057", "Thilini Fernando", "199234500789", "1992-12-10", "071-3456789", "45/2 Station Road, Dehiwala", "Dehiwala", "Accounts Executive", 142000, "Sunil Fernando", "011-2734567"],
        ["EMP-1063", "Mohamed Rizwan", "901122334V", "1990-04-21", "076-4567890", "23 Mosque Lane, Wellawatte", "Colombo 06", "Payroll Officer", 128000, "Fathima Rizwan", "076-3344556"],
        ["EMP-1071", "Dilshan Rajapaksa", "199512345678", "1995-05-02", "075-5678901", "8A Kandy Road, Kadawatha", "Kadawatha", "Junior Accountant", 96000, "Nirmala Rajapaksa", "075-1122334"],
        ["EMP-1088", "Nadeesha Kumari", "199745601122", "1997-11-18", "070-6789012", "102 Galle Road, Moratuwa", "Moratuwa", "Accounts Assistant", 78000, "Priyantha Kumara", "070-9988776"],
    ]
    policy = [["Rule", "Threshold", "Action"],
              ["Uncertified sick or unexplained absence per month", "> 3 days", "Written warning"],
              ["Uncertified sick or unexplained absence per month", "2–3 days", "Verbal reminder"],
              ["Half-day absences", "count as 0.5 day each", ""]]
    workbook("hr_leave_register_aug2026.xlsx", [
        ("Leave Register Aug 2026", header, rows, ["Finance Department — Colombo Head Office", "Leave register for August 2026 (prepared by HR, 2 Sep 2026)"]),
        ("Staff Master", master_h, master, []),
        ("Policy", policy[0], policy[1:], []),
    ])


# -------------------------------------------------- set 3: wallet support desk
def support():
    header = ["Ticket ID", "Opened", "Customer Name", "Mobile", "NIC / Passport", "Email", "Wallet Account", "Card (last txn)", "Category", "Amount (LKR)", "Channel", "Status", "Agent", "Agent Notes"]
    rows = [
        ["TCK-30412", "2026-09-01 09:12", "Sanjeewa Wijesinghe", "077-4455667", "882345678V", "sanjeewa.w@gmail.com", "PGW-100234-01", "4111 1111 1111 1111", "Failed top-up", 2500, "App", "Open", "Nuwan Peiris", "Customer says money deducted, wallet not credited. Ref 88213."],
        ["TCK-30415", "2026-09-01 11:40", "Fathima Nazreen", "071-2233445", "199456700123", "fnazreen@yahoo.com", "PGW-100987-01", "", "Duplicate charge", 1200, "Hotline", "Open", "Nuwan Peiris", "Charged twice for bus pass. Called from +94 71 223 3445."],
        ["TCK-30419", "2026-09-02 08:05", "Harsha Abeywardena", "076-9988776", "N4567890", "harsha.abey@outlook.com", "PGW-101122-02", "5555 5555 5555 4444", "Account locked", 0, "Email", "Pending customer", "Ishara Gunasekara", "Passport N4567890 used for KYC; locked after 3 wrong PINs."],
        ["TCK-30422", "2026-09-02 15:30", "Sanjeewa Wijesinghe", "077-4455667", "882345678V", "sanjeewa.w@gmail.com", "PGW-100234-01", "", "Failed top-up", 2500, "App", "Open", "Ishara Gunasekara", "Second complaint, same issue as TCK-30412. Customer angry."],
        ["TCK-30431", "2026-09-03 10:20", "Ranjith Kumar", "075-6677889", "751234567V", "ranjith.k@lankamail.lk", "PGW-102450-01", "", "Refund not received", 4300, "Hotline", "Open", "Nuwan Peiris", "Refund approved 25 Aug by Mr. Prasanna Jayasuriya, not paid."],
        ["TCK-30437", "2026-09-04 13:55", "Dilrukshi Senanayake", "070-1122334", "199012300456", "dilrukshi.s@gmail.com", "PGW-103311-01", "4012 8888 8888 1881", "Fraud suspected", 15000, "App", "Escalated", "Ishara Gunasekara", "Unknown merchant txn at 02:14. Card ending 1881. Police report pending."],
        ["TCK-30440", "2026-09-05 09:00", "Ranjith Kumar", "075-6677889", "751234567V", "ranjith.k@lankamail.lk", "PGW-102450-01", "", "Refund not received", 4300, "Email", "Open", "Nuwan Peiris", "Follow-up on TCK-30431."],
        ["TCK-30446", "2026-09-06 16:45", "Aravinth Selvam", "077-3344556", "199178901234", "aravinth.s@hotmail.com", "PGW-104020-01", "", "Failed top-up", 1000, "Hotline", "Resolved", "Ishara Gunasekara", "Credited manually. Address for card delivery: 17 Hospital Road, Jaffna."],
        ["TCK-30451", "2026-09-08 08:30", "Fathima Nazreen", "071-2233445", "199456700123", "fnazreen@yahoo.com", "PGW-100987-01", "", "Duplicate charge", 1200, "App", "Open", "Nuwan Peiris", "Still not refunded; 7 days since TCK-30415."],
    ]
    cust_h = ["Wallet Account", "Customer Name", "NIC / Passport", "Date of Birth", "Mobile", "Email", "Registered Address", "District", "KYC Level", "Wallet Balance (LKR)", "Joined"]
    cust = [
        ["PGW-100234-01", "Sanjeewa Wijesinghe", "882345678V", "1988-08-22", "077-4455667", "sanjeewa.w@gmail.com", "No. 31, Hill Street, Gampaha", "Gampaha", "Full", 6820, "2024-03-11"],
        ["PGW-100987-01", "Fathima Nazreen", "199456700123", "1994-02-05", "071-2233445", "fnazreen@yahoo.com", "112/4 Old Moor Street, Colombo 12", "Colombo", "Full", 1540, "2023-11-02"],
        ["PGW-101122-02", "Harsha Abeywardena", "N4567890", "1979-06-30", "076-9988776", "harsha.abey@outlook.com", "5 Lake Drive, Nuwara Eliya", "Nuwara Eliya", "Basic", 22900, "2025-01-19"],
        ["PGW-102450-01", "Ranjith Kumar", "751234567V", "1975-05-01", "075-6677889", "ranjith.k@lankamail.lk", "89 Main Street, Hatton", "Nuwara Eliya", "Full", 310, "2022-07-08"],
        ["PGW-103311-01", "Dilrukshi Senanayake", "199012300456", "1990-05-02", "070-1122334", "dilrukshi.s@gmail.com", "24 Temple Road, Kurunegala", "Kurunegala", "Full", 48750, "2023-04-27"],
        ["PGW-104020-01", "Aravinth Selvam", "199178901234", "1991-06-27", "077-3344556", "aravinth.s@hotmail.com", "17 Hospital Road, Jaffna", "Jaffna", "Basic", 900, "2025-06-15"],
    ]
    sla = [["Category", "Target resolution", "Escalate to"],
           ["Failed top-up", "2 business days", "Payments team"],
           ["Duplicate charge", "3 business days", "Payments team"],
           ["Refund not received", "5 business days", "Finance"],
           ["Account locked", "1 business day", "KYC team"],
           ["Fraud suspected", "Same day", "Risk & Compliance, freeze wallet"]]
    workbook("paygo_support_tickets_sep2026.xlsx", [
        ("Tickets Sep 2026", header, rows, ["PayGo Wallet — Customer Support Ticket Log, 1–8 September 2026"]),
        ("Customers", cust_h, cust, []),
        ("SLA", sla[0], sla[1:], []),
    ])


# ------------------------------------------------ set 4: insurance claim (PDF)
def claim_pdf():
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    def line(text, size=10.5, dy=16, bold=False):
        nonlocal y
        page.insert_text((56, y), text, fontsize=size, fontname="hebo" if bold else "helv")
        y += dy
    line("LANKA GENERAL INSURANCE PLC", 15, 22, True)
    line("Motor Claim Assessment Report", 12, 26, True)
    line("Claim No: MC-2026-08817          Policy No: LGI-MV-448812          Date of Loss: 28 August 2026")
    line("Assessor: Mr. Kasun Liyanage, Reg. No. ASR-0231          Report date: 4 September 2026", dy=24)
    line("1. Policyholder", 11, 18, True)
    line("Name: Mrs. Anusha Weerasinghe          NIC: 856789012V          DOB: 12 March 1985")
    line("Address: No. 27/1, Park Avenue, Nugegoda          Mobile: 077-6543210          Email: anusha.w@gmail.com")
    line("Vehicle: Toyota Aqua, Reg. CAB-4471, Chassis NHP10-6543210", dy=24)
    line("2. Third party", 11, 18, True)
    line("Name: Mr. Suresh Kanagaratnam          NIC: 199212345678          Mobile: 076-1122334")
    line("Vehicle: Bajaj three-wheeler, Reg. ABK-1290          Insurer: Ceylinco General", dy=24)
    line("3. Circumstances", 11, 18, True)
    line("On 28 August 2026 at about 7.45pm the insured vehicle, driven by the policyholder's son Dinesh Weerasinghe")
    line("(licence B1234567, NIC 200112300456), collided with the three-wheeler at the Nugegoda junction on High Level")
    line("Road. Police entry made at Mirihana Police Station, CIB 1122/26, by PC 4521 Ratnayake.", dy=24)
    line("4. Assessment", 11, 18, True)
    line("Front bumper, bonnet and left headlamp damaged. Estimated repair LKR 385,000 at Sterling Motors, Rajagiriya.")
    line("Excess LKR 25,000. Third-party damage estimated LKR 60,000. No injuries reported.", dy=24)
    line("5. Recommendation", 11, 18, True)
    line("Approve repair at LKR 385,000 less excess. Settle third-party claim of Mr. Kanagaratnam directly.")
    line("Contact for payment: account 8012345678 at Commercial Bank, Nugegoda branch, in the name of the policyholder.", dy=30)
    line("Kasun Liyanage, Senior Assessor          kasun.l@lankageneral.lk          011-2345678")
    doc.save(os.path.join(HERE, "insurance_motor_claim_report.pdf"))
    doc.close()
    print("wrote insurance_motor_claim_report.pdf")


if __name__ == "__main__":
    loans(); hr(); support(); claim_pdf()
