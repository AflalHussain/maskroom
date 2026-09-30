# Testing folder sharing on a Windows machine

Run through this once on a real Windows PC with Claude Desktop installed. Every
command is meant to be pasted one at a time so you can see what each one does;
`dev-sync.ps1` automates the file copying later, and is not used here.

## What you are testing

A folder attached to Cowork goes around every guard the helper has: Claude reads
it inside a sandbox VM and nothing of ours is in that path. So the folder is not
attached at all. Instead **SafePII serves it**, masking each file as it is read,
and an administrator policy removes every other way in. The reasoning is in
[`../docs/DESKTOP_APP_RESEARCH.md`](../docs/DESKTOP_APP_RESEARCH.md) §5.6 and
[ADR 0009](../docs/adr/0009-folder-access-is-masked-by-being-the-only-way-in.md).

Two halves, and **neither works alone**:

| Half | What it does | Without it |
|---|---|---|
| The broker, in the helper | Serves one folder, masked, on `http://127.0.0.1:47821/mcp` | Claude has no files at all |
| Claude Desktop policy | `allowedWorkspaceFolders: []` plus a `managedMcpServers` entry | The user can still attach the raw folder and SafePII never sees it |

The test is built so you can prove each half separately. **Parts 3 and 4 need no
Claude Desktop at all** — do them first, and if something breaks in Part 6 you
already know it is the policy and not the masking.

## Before you start

- Python 3.12 from python.org, `py -m pip install uiautomation`.
- Claude Desktop, signed in, working.
- A SafePII service key. Browser sign-in will not work against
  `safepii.hsenidmobile.com` yet: the live server still runs an image without
  `/auth/exchange`. Make a key on the server with
  `docker compose exec app maskroom-admin key create desktop-test`.
- The Linux box on the same network. This guide uses `192.168.101.80`; check it
  with `hostname -I` there, because it changes between networks.

---

## Part 1 — get the code across

**1.** On **Linux**, check out the branch. scp reads the working tree, so the
files on disk have to be the new ones:

```bash
cd /hms/apps/sovereign-ai/masking && git checkout feat/folder-broker
```

**2.** On **Windows**, make a folder to work in:

```powershell
mkdir $HOME\safepii-test; cd $HOME\safepii-test
```

**3.** Pull the helper:

```powershell
scp aflal@192.168.101.80:/hms/apps/sovereign-ai/masking/desktop/helper.py .
```

**4.** Pull the broker:

```powershell
scp aflal@192.168.101.80:/hms/apps/sovereign-ai/masking/desktop/broker.py .
```

Both files matter, and separately, so you know which one failed. The helper
imports `broker.py` **from its own folder**; without it the bar simply has no
"Share a folder" row and says nothing about why, so a missing file looks like a
missing feature.

**5.** Pull the sample folder:

```powershell
scp -r aflal@192.168.101.80:/hms/apps/sovereign-ai/masking/desktop/sample-folder .
```

Copy nothing else into it. Every file in that folder is a file Claude is given,
and an extra one shifts every handle along by one, which is how the numbers below
stop matching. What each sample is for is described in
[`SAMPLE-FOLDER.md`](SAMPLE-FOLDER.md), which is deliberately kept outside it.

**If any scp times out**, read it carefully: a *timeout* means the packets never
arrive, a *refusal* means they arrived and nothing was listening. Diagnose with

```powershell
Test-NetConnection 192.168.101.80 -Port 22
```

`PingSucceeded True` with `TcpTestSucceeded False` is the Linux firewall — on the
Linux box run `sudo ufw allow from 192.168.101.0/24 to any port 22 proto tcp`.
**Both False** means the two machines cannot see each other at all, which office
wifi does on purpose (client isolation) and no firewall rule will fix: use a
cable, a phone hotspot, or a USB stick for the three items above.

---

## Part 2 — run the helper

**6.** Start it, as the same Windows user that runs Claude Desktop and not
elevated:

```powershell
py helper.py
```

The settings window opens on first run. Set the server address, paste the service
key in the API key box, press **Test connection** — it should name the key — then
**Save**.

**7.** Open Claude Desktop and click into the composer. The SafePII bar appears
above it. Click the chevron.

You should see **Share a folder with Claude…** beneath the four toggles. If it is
missing, `broker.py` is not beside `helper.py`: check with `dir` and redo step 4.

---

## Part 3 — share the folder

**8.** Chevron → **Share a folder with Claude…** → pick `sample-folder`.

**9.** Read the confirmation before you accept it. It should say:

> Claude would see 5 file(s) (1 kB), masked. 3 would not be served: 1 cannot be
> checked (.png), 1 needs the document pipeline, 1 source code. File names are
> replaced with handles.

That count is arithmetic over file extensions, so it appears instantly and costs
no server call. **This is the sentence a customer's security officer will read**,
so judge it as they would.

**10.** Click **Share it**. The panel should now read **Sharing sample-folder**
with a **Stop** beside it, and a line saying how many files are being served.
Hover the pill without opening the panel: the readout should mention the folder
too, because a shared folder is a live path off the machine.

---

## Part 4 — prove the broker, with no Claude involved

Open a **second** PowerShell window for these. They talk to the broker directly,
the same way Claude Desktop will.

**11.** Ask it what tools it has:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:47821/mcp -Method Post -ContentType application/json -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Four tools: `list_files`, `read_file`, `search_files`, `write_file`.

**12.** Ask for the listing:

```powershell
(Invoke-RestMethod -Uri http://127.0.0.1:47821/mcp -Method Post -ContentType application/json -Body '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_files","arguments":{}}}').result.content[0].text
```

Expect eight files, none of them by their real name, three marked **NOT SERVED**
with a different reason each, and the subfolder as `d01/`:

```
f001.csv  (128 bytes)
f002.png  -- NOT SERVED: SafePII cannot check .png, so it is not served
f003.md   (271 bytes)
f004.xlsx -- NOT SERVED: .xlsx needs SafePII's document pipeline ...
f005.csv  (258 bytes)
f006.md   (518 bytes)
f007.py   -- NOT SERVED: source code: masking it would corrupt it ...
d01/f008.txt  (201 bytes)
```

**Handles are assigned in walk order, so match by byte size, not by number.** If
your listing has an extra file, everything after it shifts along:

| Size | Which sample it is |
|---|---|
| 128 | `Kamala_Silva_statement.csv` — the name is the only personal data |
| 271 | `branch_targets.md` — nothing personal at all |
| **258** | `loans_overdue.csv` — **the table, used in step 13** |
| **518** | `notes.md` — **the prose, used in step 14** |
| 201 | `kyc/Nimal Perera - KYC.txt`, under `d01/` |

The three refusals are named by their extension, so those are unambiguous.

**13.** Read the table — this is the one that matters most. Use **the 258-byte
`.csv`** from your own listing; it is `f005.csv` in a clean copy:

```powershell
(Invoke-RestMethod -Uri http://127.0.0.1:47821/mcp -Method Post -ContentType application/json -Body '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"f005.csv"}}}').result.content[0].text
```

Every name, NIC and mobile must be a `TOK_…`. Branch, amount and days stay as
they are. **If a name comes back in the clear, stop and send me the output**:
that is the leak the tabular route exists to prevent.

**14.** Read the prose file — **the 518-byte `.md`**, `f006.md` in a clean copy:

```powershell
(Invoke-RestMethod -Uri http://127.0.0.1:47821/mcp -Method Post -ContentType application/json -Body '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"f006.md"}}}').result.content[0].text
```

Known and expected here, measured on 2026-09-29: **"Call Nimal Perera on …" comes
back with the name in the clear** while the same name is masked in the table and
in the next paragraph, and the word *Monthly* is masked as a date. Both are
detection quality in the engine (MASK-2), not the broker, which serves what the
server returns. Everything else should be masked, and the salary and the two
ordinary dates should not be.

**15.** Check the refusal is a refusal, using the `.py` from your listing:

```powershell
(Invoke-RestMethod -Uri http://127.0.0.1:47821/mcp -Method Post -ContentType application/json -Body '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"f007.py"}}}').result.content[0].text
```

One sentence explaining why, and **no trace of `API_SECRET`**.

**16.** Check the search trick — the term is masked first, so a real name finds
its own token:

```powershell
(Invoke-RestMethod -Uri http://127.0.0.1:47821/mcp -Method Post -ContentType application/json -Body '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"search_files","arguments":{"query":"Sunil Fernando"}}}').result.content[0].text
```

It should say it searched for the token instead, and find the row in `f005.csv`.

**17.** Check a path cannot escape the folder:

```powershell
(Invoke-RestMethod -Uri http://127.0.0.1:47821/mcp -Method Post -ContentType application/json -Body '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"../../Documents/anything.txt"}}}').result.content[0].text
```

"outside the folder SafePII serves", and nothing else.

**18.** Check the write-back lands *beside* the folder, never in it. Use a token
you saw in step 13:

```powershell
(Invoke-RestMethod -Uri http://127.0.0.1:47821/mcp -Method Post -ContentType application/json -Body '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"write_file","arguments":{"path":"summary.md","content":"TOK_PERSON_XXXXXXXX owes money.\n"}}}').result.content[0].text
```

Then look at it, and confirm the original folder is untouched:

```powershell
Get-Content $HOME\safepii-test\sample-folder-safepii-out\summary.md; dir $HOME\safepii-test\sample-folder
```

The token should have become the real name in the output file, and
`sample-folder` should have gained nothing.

---

## Part 5 — point Claude Desktop at the broker

**19.** Create the policy key. `HKCU` needs no administrator, which is what you
want for a test:

```powershell
New-Item -Path HKCU:\SOFTWARE\Policies\Claude -Force | Out-Null
```

**20.** Forbid attaching folders directly. This is the half that makes SafePII
the only way in:

```powershell
Set-ItemProperty -Path HKCU:\SOFTWARE\Policies\Claude -Name allowedWorkspaceFolders -Value '[]'
```

**21.** Point it at the broker:

```powershell
Set-ItemProperty -Path HKCU:\SOFTWARE\Policies\Claude -Name managedMcpServers -Value '[{"name":"safepii-files","url":"http://127.0.0.1:47821/mcp","transport":"http"}]'
```

**22.** Quit Claude Desktop **completely** — tray icon → Quit, not just the
window — and start it again. Policy is read at startup.

**23.** Test step 20 on its own: open Cowork and try to add a folder. It should
refuse or skip it, with words like *"administrator has restricted which folders
can be used here."* If a folder still attaches, the policy did not land and
nothing below will mean much.

> The exact registry encoding for `managedMcpServers` — a JSON document in a flat
> registry value — is the one thing that could not be confirmed from the
> application package: the Linux build carries the macOS plist reader but not the
> Windows registry one. If step 24 finds no tools but Part 4 worked, this is the
> suspect, and the helper logs the shape it expects (Part 7).

**If the policy will not take, use stdio instead and carry on.** Claude Desktop's
own *Add a connector* field is not an alternative — it is for a remote server and
asks for an https address, which a loopback endpoint cannot give it. The third
door is a server Claude starts itself, which has no address at all.

**23a.** Open, or create, `%APPDATA%\Claude\claude_desktop_config.json` and put
this in it, with your own path to `broker.py`:

```json
{
  "mcpServers": {
    "safepii-files": {
      "command": "py",
      "args": ["C:\\Users\\aflal.h\\safepii-test\\broker.py", "--stdio", "--bridge"]
    }
  }
}
```

`--bridge` is the important word: this process does not serve the folder itself,
it relays to **the helper's** broker, so the folder is still served once by the
helper that holds your sign-in and the vault. If `py` is not found, use the full
path that `(Get-Command py).Source` prints.

**23b.** Quit Claude Desktop from the tray and start it again. The connector
should appear and look healthy **even with no folder shared** — that is
deliberate, since Claude starts this process before you have shared anything.
Asking it to list files then says *"Ask the person to share one from the SafePII
bar"*, which is the answer you want rather than a broken connector.

Keep `allowedWorkspaceFolders: []` from step 20 either way: it is what stops a raw
folder being attached, and it is independent of how Claude finds the broker.

---

## Part 6 — test it the way a user would

**24.** In a Cowork task, type: **"list the files you can see"**.

Claude should call the SafePII tool — expect an approval prompt the first time —
and answer with the handles. Then work through these, each of which checks
something different:

| Ask Claude | What you are checking |
|---|---|
| "read f005.csv and tell me who is most overdue" | It reasons over tokens without complaining about them |
| "search for Sunil Fernando" | The query is masked before the search |
| "what is behind TOK_PERSON_…?" | It should say it cannot know. If it guesses a name, tell me — that is the behaviour worth catching |
| "read f007.py" | It reports the refusal instead of working around it |
| "write a one-line summary to summary.md" | The output lands in `sample-folder-safepii-out`, restored |

**25.** Now the point of all of it. In Claude's reply, rest the mouse on a
`TOK_PERSON_…`. The helper's tooltip should show the **real name**. The overlay
should paint real values over the tokens.

That is the whole promise in one screen: **you see the real data, Anthropic saw
only tokens.** If the tooltip says nothing, the folder's vault did not reach the
helper — send me the log.

**26.** Copy a line of the reply and paste it into Notepad. It should paste with
real values, not tokens.

---

## Part 7 — what to send me

**27.** The log, copied rather than redirected or the line breaks are lost:

```powershell
Copy-Item $env:APPDATA\SafePII\helper.log $HOME\Desktop\helper.log
```

Useful lines to look for yourself:

- `sharing <path> on port 47821 as session …` — the folder started being served.
- `managed configuration: {"managedMcpServers": …}` — **the exact value the
  helper expects in the registry**, to compare with what step 21 set.
- `policy: ignoring unknown setting …` — a Group Policy typo.
- `vault index: N tokens across M sessions` — M should have gone up by one when
  you shared the folder. That is what makes step 25 work.

---

## Undoing it

**28.** Stop sharing from the panel, or close the helper.

**29.** Remove the policy, or Claude Desktop will keep refusing to attach folders
after you have finished testing:

```powershell
Remove-Item -Path HKCU:\SOFTWARE\Policies\Claude -Recurse -Force
```

**30.** Restart Claude Desktop for that to take effect.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No "Share a folder" row in the panel | `broker.py` is not beside `helper.py` | Redo step 4, restart the helper |
| "Cannot reach SafePII at …" | Server address or key wrong | Settings → **Test connection** |
| Confirmation says nothing can be masked | You picked a folder of code or images | Pick `sample-folder` |
| Step 11 cannot connect | Nothing is being shared, or the port is taken | Share a folder first; if the log says the port is in use, change `sharePort` in `%APPDATA%\SafePII\helper.json` **and** step 21 |
| A real name in the output of step 13 | The table went through the prose path | Send me the output — this is the important one |
| Claude sees no tools, but Part 4 worked | The registry policy did not take | Check step 23 first, then compare with the `managed configuration:` log line |
| Tooltip shows nothing on a token (step 25) | The folder's vault is not in the helper | Send the log; check the `vault index:` line |
| Sharing stopped by itself | You signed out | Expected: the broker serves with that sign-in. Share again after signing back in |
| Step 11 worked, then "Unable to connect" later | The helper was closed or restarted, or you pressed Stop | Check the panel: if it offers **Share a folder** again, nothing is being served. Locking the screen is **not** a cause — that drops the values on screen but leaves the folder served |
| Helper will not start after an update | A stale `.pyc`, or the two files disagree | Delete `__pycache__`, redo steps 3 and 4 together |

## What has not been tested anywhere yet

Everything above is the first run of this on Windows. In particular the folder
picker, the confirmation window, the `HKLM`/`HKCU` policy read and Claude Desktop
actually calling a loopback managed server have never been exercised on a real
machine — they are why this guide exists. The broker itself is covered by
`tests/test_broker.py` and the sharing flow by `tests/test_desktop_sharing.py`,
both on Linux against a fake accessibility layer.
