# Network & script inspection tool

Browser-driven capture of HTTP traffic and script payloads for any site. Uses Patchright (stealth Playwright); you drive the session, then save capture and session to disk.

## Setup

```bash
pip install -r requirements.txt
patchright install chrome
```

## Usage

```bash
python main.py
```

Browser opens on `entry.html` (instructions). Navigate to the site you want to capture in the address bar; use the site; press Enter in the terminal to save. Output is written under `output/<hostname>/`: `networkcalls.json` and `scripts.json`. Hostname is derived from captured requests (most common host) unless you pass `--url` or `--out`.

**Options**

| Option | Default | Description |
|--------|---------|-------------|
| `--url` | (none) | Force output hostname (e.g. `--url https://example.com` → `output/example.com/`) |
| `--out` | (none) | Output directory (default: `output/<hostname>/` from captured requests) |
| `--session-file` | `session.json` | Session state path |
| `--domains` | (all) | Comma-separated hosts to capture; omit = capture all |
| `--no-session` | — | Do not load or save session |
| `--user-data-dir` | (none) | Path to a persistent Chrome profile directory (see below) |

**Examples**

```bash
# Open entry, navigate manually; output dir from captured host
python main.py

# Force output to output/example.com/
python main.py --url https://example.com

# Custom output directory
python main.py --out output/my-capture

# Restrict capture to specific hosts
python main.py --domains example.com,api.example.com

# Fresh run, no saved session
python main.py --no-session

# Use a persistent Chrome profile (best for reCAPTCHA / anti-bot sites)
python main.py --user-data-dir chrome-profile
```

## Bypassing reCAPTCHA / anti-bot detection (LinkedIn, Google, etc.)

Sites that use reCAPTCHA Enterprise (LinkedIn, Google sign-up, etc.) fingerprint the browser before deciding whether to issue a valid token. In a fresh managed context they detect automation and silently skip the token — resulting in "noCAPTCHA user response code is missing or invalid".

**Fix: use a persistent Chrome profile** with `--user-data-dir`. This runs Chrome with a real on-disk profile that has browsing history, stored credentials, and a stable hardware fingerprint — exactly what reCAPTCHA scores as human.

```bash
# One-time setup: copy your real Chrome profile to a non-default path
# (Chrome's DevTools debugging rejects the default directory)
cp -r ~/.config/google-chrome ./chrome-profile

# Every subsequent run uses that profile (path is relative to this repo)
python main.py --user-data-dir chrome-profile
```

Session state (cookies, localStorage) lives in the profile directory; `session.json` is not used in this mode.

## Output

Per run, two files under `output/<hostname>/`:

- **networkcalls.json** – All captured requests/responses: `captured_at`, `total_entries`, `entries[]` (`url`, `method`, `request_headers`, `request_post_data`, `response` with `status`, `status_text`, `response_headers`, `body`).
- **scripts.json** – All scripts in one list; each entry has **`script_type`**: `"network"` | `"inline"` | `"dynamic"`.
  - **network**: same shape as networkcalls entries (url, method, request_headers, request_post_data, response with body); only JS responses.
  - **inline**: `url` (page URL), `body` (script source).
  - **dynamic**: `scriptId`, `url` (empty), `body` (source from eval/new Function via CDP).

Includes `total_scripts`, `total_network`, `total_inline`, `total_dynamic`. Full body per script; no truncation. 

## Session

Session (cookies, storage) is persisted to `--session-file` on each successful run. Next run with the same file restores it. Use `--no-session` to ignore it.
