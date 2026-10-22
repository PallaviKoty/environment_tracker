# PM2.5 Central Region Monitor

Monitors Singapore's **Central region 1-hour PM2.5** reading from the official
[data.gov.sg real-time API](https://api-open.data.gov.sg/v2/real-time/api/pm25)
and sends an hourly Gmail alert with the reading, classification, and
suggested action.

Standard-library only (no third-party dependencies).

## How it works

- Fetches the latest observation and reads `readings.pm25_one_hourly.central`.
- Classifies the reading using NEA's 1-hour PM2.5 bands (not 24-hour PSI):

  | Band      | Range (µg/m³) | Suggested action |
  |-----------|---------------|-------------------|
  | Normal    | 0–55          | Consider ventilation when outdoor conditions are suitable. |
  | Elevated  | 56–150        | Close doors/windows where practical and use a suitable air purifier if available. |
  | High      | 151–250       | Reduce outdoor particle entry and exposure; follow NEA guidance. |
  | Very High | 251+          | Minimise outdoor particle entry and follow current NEA health advisories. |

  Verify these against current NEA guidance (https://www.haze.gov.sg/) before relying on them.

- Uses the **observation timestamp** (not the computer clock) to judge
  freshness. Readings older than `MAX_READING_AGE_MINUTES` (default 150) are
  rejected as stale and no email is sent.
- Sends an email every hourly run **only when the band is Elevated or
  higher** (Normal readings are silent, no email). If the classification band
  changed since the last sent email, the email includes a prominent
  "BAND CHANGED" notice.
- A small `state.json` file records the last-notified observation timestamp
  and band, preventing duplicate emails for the same observation (e.g. on
  retries).
- Outdoor readings do not guarantee indoor air quality — the email body says
  so explicitly.

## Requirements

- Python 3.11+
- A Gmail account with an **App Password** (see below)

## 1. Gmail setup

Gmail requires an **App Password** for SMTP from scripts (your normal
password will not work if 2-Step Verification is on, which is required to
create an App Password):

1. Enable 2-Step Verification: https://myaccount.google.com/security
2. Create an App Password: https://myaccount.google.com/apppasswords
   (choose "Mail" / "Other", then copy the 16-character password).
3. Use that password as `GMAIL_APP_PASSWORD` — never your normal login password.

If App Passwords aren't available (e.g. Workspace admin restrictions), ask
your admin to allow them, or use a non-Gmail SMTP provider (e.g. SendGrid,
Mailgun, Outlook) by adjusting `send_email()` in `main.py` — out of scope for
this minimal implementation.

## 2. Local setup (Windows)

```powershell
cd c:\Users\PKOTY\PycharmProjects\environment_tracker
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env   # fill in GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ALERT_RECIPIENT_EMAIL
```

Load `.env` into the current shell (PowerShell has no built-in dotenv loader):

```powershell
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') {
        [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim())
    }
}
```

## 3. Dry-run testing (safe, default)

No real email is ever sent unless `ENABLE_LIVE_EMAIL=true` is set **and**
`--dry-run` is not passed. By default (no env var, no flag) the app
automatically falls back to dry-run and logs a warning.

```powershell
python main.py --dry-run --once      # single cycle, prints the email instead of sending
python main.py --test-email --dry-run # simulate a test email
```

Run the automated tests:

```powershell
python -m unittest discover -s tests -v
```

## 4. Sending real emails

Set `ENABLE_LIVE_EMAIL=true` (in `.env` / environment) and omit `--dry-run`:

```powershell
$env:ENABLE_LIVE_EMAIL = "true"
python main.py --test-email      # sends one real test email, verifies SMTP creds
python main.py --once            # single real check-and-notify cycle
python main.py                   # runs forever, one cycle per hour (Ctrl+C to stop)
```

For local hourly scheduling without keeping a terminal open, use **Windows
Task Scheduler** to run `python main.py --once` every hour instead of the
built-in loop.

## 5. GitHub Actions deployment

1. Push this repository to GitHub.
2. Add repository secrets (Settings → Secrets and variables → Actions):
   - `GMAIL_ADDRESS`
   - `GMAIL_APP_PASSWORD`
   - `ALERT_RECIPIENT_EMAIL`
3. For multiple recipients, set `ALERT_RECIPIENT_EMAIL` to a comma-separated
   list, e.g. `alice@example.com,bob@example.com` (works the same locally
   via `.env`).
4. The workflow `.github/workflows/pm25-monitor.yml` runs hourly via
   `cron: "5 * * * *"` (UTC). Since Singapore is UTC+8, this fires at `HH:05`
   SGT, 5 minutes after the top of the hour, giving NEA's API time to publish
   the reading. GitHub may delay scheduled runs further; `MAX_READING_AGE_MINUTES`
   (150) tolerates typical delays without sending stale data.
5. `concurrency: group: pm25-monitor` prevents overlapping runs if a run is
   delayed into the next scheduled slot.
6. You can also trigger a run manually via **Actions → PM2.5 Hourly Monitor →
   Run workflow**. Tick the "Simulate only" input to perform a safe dry run
   (no real email, no state file change) directly on GitHub's runners —
   useful if your local machine can't reach the internet directly (e.g.
   behind a restrictive corporate proxy).

### Persistent duplicate-prevention state

The workflow commits `state.json` back to the repository after each run (only
when it changes), using the built-in `GITHUB_TOKEN` and `contents: write`
permission. This is the simplest reliable way to persist state across
ephemeral Actions runners without external services. `state.json` is the only
file written; `.env`, credentials, and secrets are never committed (see
`.gitignore`).

### Free-tier limitations

- GitHub Actions free tier (public repos: unlimited minutes; private repos:
  limited monthly minutes) — hourly runs are lightweight (seconds each) and
  should comfortably fit typical free quotas for a private repo.
- Scheduled workflows can be delayed during high GitHub load; delays beyond
  `MAX_READING_AGE_MINUTES` cause the run to safely skip sending (logged as
  stale), rather than sending outdated data.
- Gmail SMTP has sending limits (~500/day for regular Gmail accounts), which
  an hourly job (24/day) is well within.

## Files

- `main.py` — fetch, classify, email, CLI, loop
- `requirements.txt` — dependency notes (standard library only)
- `.env.example` — template for required environment variables
- `.gitignore` — excludes `.env`, `state.json`, venv, caches
- `.github/workflows/pm25-monitor.yml` — hourly GitHub Actions schedule
- `tests/test_main.py` — unit tests (boundaries, invalid/stale data, messages, dry-run safety)
