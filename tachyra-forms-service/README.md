# Tachyra Forms — setup guide

Four website pages now have their forms built into the page design instead of WPForms:

| Page | File | Form name |
|---|---|---|
| Apply for the Pilot (`/pilot-beta/`) | tachyra-pilot-beta-page-COMPLETE.txt | `pilot` |
| Physician Directory (`/physician-directory/`) | tachyra-physician-directory-page-COMPLETE.txt | `directory` (physician listing) and `directory_notify` (patients) |
| Founding Physicians (`/founding-physicians/`) | tachyra-founding-physicians-page-COMPLETE.txt | `founding` |
| Seed Round (`/seed-round/`) | tachyra-seed-round-page-COMPLETE.txt | `investor` |

Each form sends to a small service, **Tachyra Forms** (`app.py`). It checks each submission, blocks spam, saves it in a database, and emails a notification to info@tachyradiagnostics.com. Nothing is lost if email fails, because every submission is saved first.

**Do the steps in order. The pages won't work until the service is running (steps 1–5).**

---

## 1. Put the service code on GitHub

Create a new private repository named `tachyra-forms` (in the Tachyra organization once it exists). Add `app.py`, `requirements.txt` and `test_app.py` from this folder.

## 2. Create a small database on Render

Render → **New → Postgres**
- Name: `Tachyra-Forms-DB`
- Plan: Basic (smallest)
- Region: Oregon (same as the other services)
- Under **Networking**, remove every entry from the IP allow list. The service connects from inside Render, so no outside access is needed.

Keep this separate from the pilot database. It will hold physician and investor contact details, not patient data.

## 3. Create the web service on Render

Render → **New → Web Service** → connect the `tachyra-forms` repository
- Name: **`tachyra-forms`**. The pages expect `https://tachyra-forms.onrender.com`; if you use a different name, replace that address in all four page files.
- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT`
- Plan: **Starter**. The free plan goes to sleep, and the first visitor would wait about a minute, long enough for the form to fail.

## 4. Add the settings (Environment tab)

Never put these values in code or email.

| Name | Value |
|---|---|
| `DATABASE_URL` | The **Internal Database URL** of `Tachyra-Forms-DB` |
| `ALLOWED_ORIGINS` | `https://tachyradiagnostics.com,https://www.tachyradiagnostics.com` |
| `ADMIN_TOKEN` | A long random password (40+ characters). Needed to download entries. Share it only with Barry. |
| `SMTP_HOST` | Microsoft 365: `smtp.office365.com` · GoDaddy Professional Email: `smtpout.secureserver.net` |
| `SMTP_PORT` | Microsoft 365: `587` · GoDaddy Professional Email: `465` |
| `SMTP_USER` | The mailbox that sends notifications, e.g. info@tachyradiagnostics.com |
| `SMTP_PASS` | That mailbox's password |
| `NOTIFY_TO` | info@tachyradiagnostics.com |

For Microsoft 365, "Authenticated SMTP" may need to be turned on for that mailbox in the Microsoft 365 admin center (Users → the mailbox → Mail → Manage email apps).

## 5. Check the service

Open `https://tachyra-forms.onrender.com/healthz`. It should show `{"ok": true}`.

## 6. Install the pages in WordPress

For each of the four pages:
1. Open the page in WordPress and remove everything on it, including the WPForms block.
2. Add **one Custom HTML block** and paste the entire contents of the page's `.txt` file.
3. **Founding Physicians only:** upload Barry's and Annie's photos to the Media Library, copy each file's URL, and replace `BARRY-PHOTO-URL` and `ANNIE-PHOTO-URL` in the code.
4. Click **Update**, clear the site cache, and check the page on a computer and a phone.

Keep the WPForms plugin installed for now: the Refer a Colleague page still uses it.

## 7. Test each form

Submit one test entry on each page (wait a few seconds before submitting; instant submissions are treated as spam and silently discarded). Confirm that:
- The page shows its thank-you message
- A notification arrives at info@tachyradiagnostics.com

## 8. Download entries

Entries can be downloaded as a spreadsheet (CSV) by anyone with the admin token:

```
curl -H "X-Admin-Token: YOUR-ADMIN-TOKEN" https://tachyra-forms.onrender.com/export/founding.csv -o founding.csv
```

Replace `founding` with `pilot`, `directory`, `directory_notify` or `investor`.

## Built-in protections

- Only tachyradiagnostics.com can send to the service
- Hidden spam trap and minimum fill time; 8 submissions per 10 minutes per visitor
- Every field checked on the server: required fields, valid email, 10-digit NPI, ZIP code, allowed choices, length limits
- Visitor IP addresses are never stored, only a scrambled code used for the spam limit
- Form contents are never written to the logs
- Each form tells visitors not to include patient information; the investor form also says not to enter Social Security or account numbers

## Running the tests

`python3 test_app.py` should print `ALL TESTS PASSED`.
