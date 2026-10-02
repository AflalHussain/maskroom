# For users, before SafePII arrives

Send this before the install lands, not after. A privacy tool that appears
unannounced gets reported to the service desk as malware, and the person
reporting it is not wrong to.

Edit the names and the contact; everything else is written to be sent as it is.

---

**Subject: A change to how Claude handles company data on your laptop**

From this week a tool called **SafePII** runs alongside Claude on your machine.

It replaces personal data — names, NIC numbers, phone numbers, account numbers —
with placeholders before anything leaves your laptop, and shows you the real
values on your own screen. So you read what you expect to read, and Claude
receives something that cannot identify anyone.

**What you will see.** A small bar above the message box in Claude. It shows what
SafePII is doing, and opens into a panel with the last few things it did.

**What changes.** Claude can no longer open folders on your computer by itself.
Ask it to work with your files and it will ask you to choose a folder; SafePII
then hands it those files with the personal data already replaced. Replies may
show placeholders like `TOK_PERSON_4A66D755` — rest your mouse on one and you
will see the real value. Copy the text and it pastes with the real values too.

**What has not changed.** Nothing about how you use Claude otherwise, and nothing
about who can see your work.

**If something looks wrong**, open the panel from the bar — it lists what
happened most recently, and anything that needs your attention stays there until
you dismiss it. If a message will not send, SafePII is holding it because it
could not check it; the panel says so.

Questions to <your IT contact>.

---

## Notes for whoever sends it

- **Send it before the policy lands.** The first thing users notice is that
  Claude can no longer open their folders, and with no explanation that reads as
  a fault.
- **Do not describe it as monitoring.** It is not: the helper reads the message
  box and the files the person chooses to share, and sends them to your own
  SafePII server. It does not watch what else they do. Say so if asked, because
  somebody will ask.
- **Name a person to ask.** A tool nobody will answer questions about gets
  worked around.
