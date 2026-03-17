# DevOps Helper (Labs)

Internal Flask dashboard for DevOps tasks on Linux in a trusted network.

## Why this version

- Lightweight: single `app.py`, SQLite file, and two templates.
- Easy to maintain: plain Flask routes, no extra framework layers.
- Junior-friendly: each feature is visible and readable in one place.

## Features

- Log search over SQLite deployment logs
- Predefined server diagnostics commands over SSH
- `.env` key/value management
- Dynamic message rendering with Jinja2 templates
- Basic action logging to `actions.log`

## Project layout

- `app.py` - main Flask app and all route logic
- `templates/index.html` - dashboard UI
- `templates/result.html` - command/template output page
- `devops_helper.db` - SQLite DB (created on first run)
- `.env` - environment key/value file (created when saving vars)
- `actions.log` - local file log of user actions

## Run locally (Linux)

1. Change into the app directory:

```bash
cd labs/v0-insecure
```

2. Install dependencies with `uv`:

```bash
uv sync
```

3. Start app:

```bash
uv run app.py
```

4. Open in browser:

`http://127.0.0.1:5000`

## Notes

- This app assumes a trusted internal environment and is intentionally minimal.
- Diagnostic execution is limited to predefined commands in `DIAGNOSTIC_COMMANDS`.
