# Chrome Browser Cloud Management (Admin console) — no files to host if using the Web Store

Admin console → **Devices → Chrome → Apps & extensions → Users & browsers**.

## Force-install
Add the extension by ID `lcmdehcdpfddkjgajmlpgfholdekpgio`.
- If **self-hosted**: choose "Add Chrome app or extension by URL" and give the
  update.xml URL (`https://EXTENSIONS.YOURCO.EXAMPLE/maskroom/update.xml`).
- If **unlisted Web Store**: add by ID; update URL is Google's default.
Set **Installation policy = Force install**, and **Pin to toolbar** on.

## Lock the guard
Select the Maskroom extension → **Policy for extensions** → paste:

```json
{ "guardLocked": { "Value": true }, "serverUrl": { "Value": "https://maskroom.yourco.example" } }
```

## Disable Incognito (closes the bypass)
Settings → **Security → Incognito mode = Disallow incognito mode**
(policy `IncognitoModeAvailability = 1`).

Verify on a managed machine at `chrome://policy` → Reload policies.
