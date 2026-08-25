# Fluent Plus

A lightweight desktop app to log in to Jenkins with an API token and run
test-case jobs — no third-party packages, no database.

## Stack
- **GUI:** `tkinter` (Python standard library)
- **Jenkins API calls:** `urllib` (Python standard library)
- **Storage:** a local JSON file at `~/.fluent_plus/profiles.json` (no DB)

## Requirements
- Python 3.8+ (no `pip install` needed — everything used is built-in)

## Run it
```bash
python3 app.py
```

## Getting a Jenkins API token
1. Log in to Jenkins in your browser.
2. Go to `<jenkins-url>/user/<your-username>/security/`.
3. Under **API Token**, click **Add new Token**, name it, and copy the value.
4. Use your Jenkins **username** + this **token** (not your password) to log in to Fluent Plus.

## What it does
- **Login screen** — enter Jenkins URL, username, and API token. Optionally
  save the profile locally for next time (token is base64-obfuscated in the
  JSON file — not strong encryption, just avoids storing it in plain sight).
- **Job list** — shows all jobs on the Jenkins server with their current
  status (PASS / FAIL / UNSTABLE / RUNNING / etc.), with a filter box.
- **Run a job** — select a job and click "Run Selected". If the job takes
  parameters, a dialog collects them first.
- **Live console output** — after triggering, the app polls Jenkins and
  streams the console log into the right-hand pane until the build finishes,
  then shows the final result.

## Files
| File               | Purpose                                            |
|--------------------|-----------------------------------------------------|
| `app.py`           | tkinter GUI (login window + main window)            |
| `jenkins_client.py`| Jenkins REST API wrapper built on `urllib`           |
| `storage.py`       | Local JSON-file profile storage (no database)        |

## Notes / things to decide next
- Currently polls every 3 seconds for build status and console output
  (`POLL_INTERVAL_SECONDS` in `app.py`) — adjust if you want it snappier
  or lighter on the Jenkins server.
- If your Jenkins uses a reverse proxy with a self-signed cert, you may
  need to adjust `jenkins_client.py` to point to a custom CA bundle via
  `ssl` (also stdlib) — happy to add that if it applies to you.
- No packaging step yet (e.g., PyInstaller) since that would be a
  third-party tool — if you want a standalone `.exe`/`.app`, let me know
  how you'd like to handle that constraint.
