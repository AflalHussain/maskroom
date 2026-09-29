# Source code. SafePII must refuse to serve this rather than mask it:
# masking would corrupt it, and the identifiers below are the kind of thing a
# name recognizer mistakes for a person.
API_SECRET = "do-not-let-this-leave-the-machine"
BRANCHES = {"Colombo": 1, "Kandy": 2, "Galle": 3}


def reconcile(rows, silva_adjustment=0):
    return sum(r["amount"] for r in rows) + silva_adjustment
