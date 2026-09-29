# `sample-folder/` — a folder to share with Claude, for testing

Eight files, each there to exercise one path through the file broker
([`broker.py`](broker.py)). Share [`sample-folder/`](sample-folder/) from the bar
and compare what comes back with the table below. The full walkthrough is
[`TESTING-FOLDER-SHARING.md`](TESTING-FOLDER-SHARING.md).

This description lives **outside** the folder on purpose. Anything inside it is a
file Claude is given: it would be masked and served like the rest, it would take
a handle of its own and shift every other handle along by one, and it would tell
Claude what the test is about.

Everything here is invented. The names, NICs, mobile numbers and accounts follow
Sri Lankan formats so the locale recognizers have something real to work on, but
no person or account in it exists.

| File | What it is for | What should come back |
|---|---|---|
| `notes.md` | Prose with names, NICs, mobiles, a street address, a birth date, a salary and two ordinary dates | Masked through the free-text path. The salary and the ordinary dates stay as they are — they are analytical fields; the birth date and the street address do not |
| `loans_overdue.csv` | A table whose **names are in rows with no sentence around them** | Masked through the *tabular* pipeline. If a name comes back in the clear, the file went through the prose path and that is the bug this file exists to catch |
| `Kamala_Silva_statement.csv` | A file whose **name** is the personal data; the contents hold none | Contents unchanged. The name must not appear: by default it comes back as a handle such as `f001.csv` |
| `kyc/Nimal Perera - KYC.txt` | A name **inside a path**, in a subfolder | Served as `d01/f008.txt`. Under `--names mask` the path is masked one component at a time, because a separator glues the folder to the name and defeats the detector |
| `branch_targets.md` | No personal data at all | Byte for byte what it was. A file that comes back changed means something is being masked that should not be |
| `reconcile.py` | Source code, with a secret and a variable named after a person | **Refused.** Masking it would corrupt it, and `silva_adjustment` is exactly what a name recognizer trips over. The secret must never appear |
| `leave_register.xlsx` | A spreadsheet | **Refused** — it needs the server's document pipeline, which the broker does not call yet |
| `branch_logo.png` | One binary pixel | **Refused** — nothing here can check it |

## The property worth checking

The same value gets the same token everywhere in the folder. `912345678V` appears
in `notes.md`, in `loans_overdue.csv` and in the KYC file, and all three should
come back as the *same* `TOK_LK_NIC_…`. That is what lets `search_files` find a
real name: the term is masked first, and a deterministic token matches itself.

## Two things the samples are good at showing, and both are the engine's

Measured against a live server on 2026-09-29, not assumed:

- **A name can be missed in prose.** In `notes.md`, "Call Nimal Perera on …" came
  back with the name in the clear, while the same name in the table and in the
  next paragraph was masked. That is detection recall, tracked as MASK-2 in
  [`../docs/PRODUCTION_READINESS.md`](../docs/PRODUCTION_READINESS.md), and
  not something the broker can fix — it serves what the server returns.
- **A false positive costs nothing but looks odd.** "Monthly salary 185,000" came
  back as "`TOK_DATE_TIME_…` salary 185,000": the word *Monthly* was read as a
  date. The salary itself was left alone, which is the intended policy.

Neither is a reason to stop; both are worth seeing before a customer does.
