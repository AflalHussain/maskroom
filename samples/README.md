# Test & demo samples

Prompt sets and files for exercising the LLM staging workflow end to end: mask → send to
Claude → unmask the reply. Use them with the **staging page** (`/staging`) or the
**browser extension** (`extension/`). Every person, number and address here is invented;
the card numbers are the public Luhn-valid test numbers. Regenerate the files any time with:

```bash
pii_env/bin/python samples/generate_samples.py
```

| Set | Scenario | Files | Exercises |
|---|---|---|---|
| [0](#set-0--two-minute-smoke-test) | Smoke test | – | text mask, one reply, unmask |
| [1](#set-1--loan-collections) | Loan collections | [`loans_overdue_register.xlsx`](loans_overdue_register.xlsx) | names, NICs, phones, emails, account numbers, a card number, a note with a spouse and a guarantor |
| [2](#set-2--hr-leave-register) | HR leave register | [`hr_leave_register_aug2026.xlsx`](hr_leave_register_aug2026.xlsx) | title rows above the header, stacked sheets, birth dates, home addresses, a manager named inside a remark |
| [3](#set-3--wallet-support-desk) | Wallet support desk | [`paygo_support_tickets_sep2026.xlsx`](paygo_support_tickets_sep2026.xlsx) | repeat tickets, passports, cards, free-text notes with phones and names, SLA reasoning over dates |
| [4](#set-4--insurance-claim-pdf) | Insurance claim (PDF) | [`insurance_motor_claim_report.pdf`](insurance_motor_claim_report.pdf) | PDF text mode, several people in prose, vehicle and case numbers that must survive |
| [5](#set-5--unmask-robustness) | Unmask robustness | – | lowercased, spaced, escaped and truncated tokens; unknown tokens |

**How a set runs.** Message 1 is typed text: paste it into the composer and mask it (the
extension's *Mask* button or guard, or step 1 on the staging page and *copy for Claude*).
Message 2 is a continuation: attach the file with the extension's *Mask file* button (or
mask it in step 2 on the staging page and upload the masked output yourself), then send
the prompt in the same chat. Replies are restored on screen by the extension, or by pasting
them into step 3 on the staging page. Both messages of a set must use the **same session**
so file tokens and text tokens share one vault.

**Reading the "expect" notes.** "Masked" means the value becomes a `TOK_<TYPE>_<ID>` token
and comes back as the original after unmasking. "Stays visible" means the policy keeps it
readable on purpose (amounts, event dates, cities, job titles, product codes) *or* it is a
detection miss — the misses are called out explicitly, because they are what a demo
audience will ask about. Counts below come from running the engine on these exact files
with the default settings (`lg` model, `dates=birth`, `locations=address`, locale `lk`).

---

## Set 0 — two-minute smoke test

**Message 1**

```
Nimal Perera (NIC 853421234V, phone 077-1234567) has not paid his loan for 45 days. Draft a two-sentence reminder message to him that mentions his NIC and phone number for verification.
```

**Expect:** 3 tokens (person, NIC, phone). The reply must contain all three, because the
prompt asks for them; the extension restores them in place, or paste the reply into the
staging page and see three green restored values.

---

## Set 1 — loan collections

**Message 1**

```
Below are three overdue loan accounts from our Kandy branch. For each customer:
1. Write a short, polite SMS (under 160 characters) reminding them of the overdue amount, addressing them by name and quoting their account number.
2. Then give me a table with columns: customer, NIC, phone, account, days overdue, suggested action.

Customers:
- Nimal Perera, NIC 853421234V, phone 077-1234567, email nimal.perera@gmail.com, account ABC-1234-002, 45 days overdue, balance LKR 182,500, address No. 45, Galle Road, Colombo 03.
- Kumari Bandara, NIC 199085601234, phone 071-9876543, email kumari.b@lankamail.lk, account ABC-1234-017, 12 days overdue, balance LKR 64,000, address 12/3 Peradeniya Road, Kandy.
- Ruwan Jayawardena, NIC 912345678V, phone +94 76 555 1234, email ruwan.j@hotmail.com, account ABC-5678-003, 90 days overdue, balance LKR 410,000, address 88 Temple Lane, Negombo.
```

**Message 2** — attach [`loans_overdue_register.xlsx`](loans_overdue_register.xlsx)

```
Attached is the full overdue register for the same portfolio, with our collections policy on the second sheet. Continuing from the three customers above:

1. Apply the policy to every account and list the action for each: customer name, account number, branch, days overdue, balance, action.
2. Two accounts already have a guarantor or family member noted in the collections note. Name those contacts, with the phone number recorded for each, and say whether we may contact them under our policy of "primary borrower first".
3. Draft the final notice letter for the account that is over 90 days, addressed by name, quoting the account number and NIC.
4. Which branch carries the largest overdue balance in total?

Keep every identifier exactly as written in the sheet.
```

**Expect (file):** 37 cells masked — 8 person, 7 NIC, 8 phone, 6 email, 6 account,
1 card, 1 address. The customers in Message 1 get the **same tokens** in the file, because
the session vault is shared. Branch names, products, dates, balances and days overdue stay
visible, so questions 1 and 4 remain answerable. The spouse and the guarantor named inside
the notes are masked, as is the guarantor's NIC and the landline in the note.

**Known misses to point out:** the employer "Ceylon Tea Exports" stays visible (an
organisation, not masked by default), and in the field-visit note the street "88 Temple
Lane" stays while "Negombo" is tokenised — the reverse of the intended address policy for
this particular sentence.

---

## Set 2 — HR leave register

**Message 1**

```
Here is the attendance and leave record for our Colombo finance team for August. Please:
1. Identify who has exceeded 3 days of unplanned leave and write a one-paragraph email to each of them from HR, addressing them by name and quoting their employee ID and the dates concerned.
2. Produce a table: employee, employee ID, NIC, mobile, total leave days, unplanned days, action (none / verbal reminder / written warning).
3. Finish with a two-line summary for the department head, Mrs. Sanduni Wickramasinghe, naming anyone who needs a written warning.

Staff:
- Chaminda Silva, EMP-1042, NIC 871234567V, mobile 077-2345678, sick leave 12, 13, 14 Aug (uncertified), casual leave 20 Aug, annual leave 25-27 Aug.
- Thilini Fernando, EMP-1057, NIC 199234500789, mobile 071-3456789, annual leave 4-8 Aug, no unplanned leave.
- Mohamed Rizwan, EMP-1063, NIC 901122334V, mobile 076-4567890, uncertified sick leave 6, 7, 19, 21 Aug, casual leave 28 Aug.
- Dilshan Rajapaksa, EMP-1071, NIC 199512345678, mobile 075-5678901, half-day absences 11, 18, 25 Aug, casual leave 1 Aug.

Company policy: more than 3 days of uncertified sick or unexplained absence in a month triggers a written warning; 2 to 3 days a verbal reminder.
```

**Message 2** — attach [`hr_leave_register_aug2026.xlsx`](hr_leave_register_aug2026.xlsx)

```
Attached is the finance team's August leave register with a staff master sheet and our leave policy.

1. Using the Policy sheet, work out who needs a written warning and who needs a verbal reminder. Show your count of uncertified or unexplained days per person, treating half-day absences as 0.5.
2. Draft the written warning letter(s). Address each person by name, quote their employee ID and NIC, list the dates concerned, and copy the department head, Mrs. Sanduni Wickramasinghe.
3. For anyone on a verbal reminder, write a two-line note for their team lead with the employee's name and mobile number.
4. Finish with a table: employee name, employee ID, designation, city, uncertified days, action.

Do not change the identifiers as written in the sheet.
```

**Expect (file):** 100 cells masked — 27 person, 21 NIC, 26 phone, 16 email, 5 birth
dates, 5 home addresses. The two title rows above the header are handled (the header is
found on row 4). Employee IDs, leave dates, designations, cities, salaries and the policy
sheet stay visible. The manager named inside a remark ("Approved by Mrs. Sanduni
Wickramasinghe") is masked, and so are four of the five emergency contacts.

**Known miss to point out:** the emergency contact "Priyantha Kumara" on the last row of
*Staff Master* stays visible — the column header does not match a name rule and the name
recogniser did not catch that one. If the reply mentions it, it appears as plain text, not
as a restored token.

---

## Set 3 — wallet support desk

**Message 1**

```
I run support for PayGo Wallet. Three complaints came in this morning and I need help triaging them before the 10am stand-up.

1. Sanjeewa Wijesinghe (wallet PGW-100234-01, mobile 077-4455667, NIC 882345678V) topped up LKR 2,500 from his card 4111 1111 1111 1111 on 1 Sep. The money left his bank but the wallet was never credited. Reference 88213. He has now complained twice and says he will post about it on Facebook.
2. Fathima Nazreen (PGW-100987-01, 071-2233445, fnazreen@yahoo.com) was charged LKR 1,200 twice for a bus pass on 1 Sep and is still waiting for the refund.
3. Dilrukshi Senanayake (PGW-103311-01, 070-1122334) reports an unknown merchant transaction of LKR 15,000 at 02:14 on 4 Sep on her card ending 1881. She has not filed a police report yet.

For each customer: classify the ticket, tell me the team it should go to, and draft a reply SMS under 160 characters that addresses them by name and quotes their wallet account. Flag which one is most urgent and why.
```

**Message 2** — attach [`paygo_support_tickets_sep2026.xlsx`](paygo_support_tickets_sep2026.xlsx)

```
Here is the full ticket log for 1–8 September, with the customer master and our SLA sheet. Continuing from the three cases above:

1. Which open tickets have breached their SLA as of today, 9 September? List them with ticket ID, customer name, mobile, category and days open.
2. Two customers have opened repeat tickets for the same problem. Name them, quote both ticket IDs, and draft an apology email for each that mentions their wallet account and the amount involved.
3. Which agent holds the most open tickets? Give their name and the count.
4. Produce a table: customer name, wallet account, district, KYC level, open tickets, oldest open ticket, recommended action.

Keep every identifier exactly as it appears in the sheet.
```

**Expect (file):** 101 cells masked — 23 person, 16 phone, 15 NIC, 15 email, 15 wallet
accounts, 3 cards, 1 passport, 7 addresses, 6 birth dates. Free-text notes are handled:
the "+94 71 223 3445" callback number, the approver "Mr. Prasanna Jayasuriya" and the
passport quoted in a note all become tokens. Ticket IDs, categories, amounts, districts,
KYC levels, dates and the SLA sheet stay visible.

**Known misses to point out:** the agent "Ishara Gunasekara" stays visible in the *Agent*
column while "Nuwan Peiris" is masked, so question 3 may return a real name that was never a
token. In the note "17 Hospital Road, Jaffna" the street stays and the town is tokenised.
The title row "PayGo Wallet — Customer Support Ticket Log" has "PayGo Wallet" tokenised as
a person (over-masking in a title cell; harmless but visible).

---

## Set 4 — insurance claim (PDF)

**Message 1** — attach [`insurance_motor_claim_report.pdf`](insurance_motor_claim_report.pdf)
(the extension attaches it as masked Markdown; on the staging page choose the file and copy
the masked text)

```
Attached is an assessor's report for a motor claim. Please:
1. Summarise the claim in five bullet points: who is insured, who the third party is, what happened, what it costs, and what the assessor recommends.
2. Draft the settlement letter to the policyholder, addressed by name, quoting the claim number, policy number, vehicle registration and the bank account the payment goes to.
3. Draft a short letter to the third party's insurer about their client's damage, naming the third party and quoting their vehicle registration.
4. List every person mentioned in the report and their role.
```

**Message 2** — text only, same chat

```
The policyholder called on 077-6543210 to say her son Dinesh Weerasinghe was not the driver — she was. Redraft point 2 of the settlement letter to reflect that, and tell me whether the licence number quoted in the report (B1234567) still matters for the claim.
```

**Expect (file):** 17 distinct values tokenised — the policyholder, third party, driver,
assessor and their NICs, phones and emails, the birth date, the home address and the bank
account. Claim, policy and vehicle numbers, the chassis number, the garage, the police
station, amounts and dates stay visible, so the letters remain usable. Message 2 reuses the
same phone number and the son's name, so they get the **same tokens** as in the file.

**Known misses to point out:** the police constable "PC 4521 Ratnayake" stays visible (a
single surname after a rank is not caught), the driving licence "B1234567" stays visible
(no licence recogniser), and the assessor's office landline "011-2345678" in the sign-off
stays visible while the mobiles are masked.

---

## Set 5 — unmask robustness

Run after any set above, on the staging page's step 3 (or watch the extension's on-screen
restore). Take one real token from your masked text, e.g. `TOK_PERSON_8B584CCF`, and paste
a "reply" that mangles it in the ways models actually do:

```
Summary for tok_person_8b584ccf: overdue.
The same customer, TOK PERSON 8B584CCF, was contacted twice.
In markdown: **TOK\_PERSON\_8B584CCF** promised to pay.
Truncated by the model: TOK_PERSON_8B584C.
Never issued: TOK_PERSON_99999999.
```

**Expect:** the first four restore to the same name (the last three are reported as
near-matches with the token they resolved to); the never-issued token is listed as
**unresolved** and left in place — the tool never guesses.

---

## Suggested demo order (about ten minutes)

1. **Set 0** on the staging page: mask, show the token list, copy for Claude, paste the
   reply back, show the restored values. Establish the round trip.
2. **Set 3, Message 1** with the extension: type the text, press Enter, show the guard
   masking it and stopping the send, press Enter again. Point at the on-screen restore when
   the reply arrives.
3. **Set 3, Message 2**: *Mask file*, show the attachment name ending in `_masked`, send the
   prompt, show the restored table. Then point at the agent name that was never masked —
   pseudonymisation is not anonymisation, and this is why an organisation still needs an
   enforcement layer.
4. **Set 4**: a PDF, to show black boxes are not what an LLM needs.
5. **Set 5**: paste the mangled reply to show tolerant restore and the unresolved report.
