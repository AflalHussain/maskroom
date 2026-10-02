#!/bin/bash
# Assemble what an administrator is given, into one folder.
#
#   ./enterprise/make-handover.sh              # -> ./handover
#   ./enterprise/make-handover.sh /tmp/out     # -> /tmp/out
#
# Assembled rather than kept as a second copy in the tree: an .admx that drifts
# from the one the helper is tested against is worse than no .admx. The MSI is
# not here -- it is built on Windows, and the caller adds it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$(cd "$HERE/.." && pwd)/handover}"

mkdir -p "$OUT/en-US"
cp "$HERE/policies/windows/admx/SafePII.admx"        "$OUT/"
cp "$HERE/policies/windows/admx/en-US/SafePII.adml"  "$OUT/en-US/"
cp "$HERE/WINDOWS-RUNBOOK.md"                        "$OUT/"
cp "$HERE/USER-NOTICE.md"                            "$OUT/"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$HERE/../desktop/helper.py")
cat > "$OUT/READ-ME-FIRST.txt" <<TXT
SafePII desktop helper — what is in here
========================================

  SafePIIHelper-${VERSION}.msi   the helper. NOT INCLUDED: built on Windows,
                               add it beside this file before sending.
  SafePII.admx                 Group Policy template  -> SYSVOL\\...\\PolicyDefinitions\\
  en-US\\SafePII.adml            its strings           -> ...\\PolicyDefinitions\\en-US\\
                               The en-US folder is not optional: an .adml in the
                               wrong place makes the editor reject the template
                               without saying why.
  WINDOWS-RUNBOOK.md           how to deploy it, what it enforces and what it
                               does not. Read this first.
  USER-NOTICE.md               a paragraph to send users BEFORE the policy
                               lands, and notes for whoever sends it.

Three things that are not in any file:

  1. This build is NOT code-signed. It is a pilot build, for machines you
     control. In application allowlisting it needs a hash rule, not a publisher
     rule. Do not take it past the pilot group.
  2. Under Group Policy, do NOT pass SERVERURL to the MSI -- Software
     Installation has no command line. Set the server address through the ADMX
     instead. The property is for Intune and msiexec only.
  3. The Claude Desktop policy in Step 3 of the runbook goes out WITH the
     helper, not before or after. Either half on its own is worse than neither.

Built from SafePII ${VERSION}.
TXT

echo "Assembled in $OUT:"
find "$OUT" -type f | sed "s|$OUT/|  |" | sort
echo
echo "Add the MSI, then:  zip -r safepii-handover.zip \"$(basename "$OUT")\""
