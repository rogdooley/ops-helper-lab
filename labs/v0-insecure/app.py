import logging
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for
from jinja2 import Template, TemplateError

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "devops_helper.db"
ENV_FILE = BASE_DIR / ".env"
ACTION_LOG_PATH = BASE_DIR / "actions.log"
ALLOWED_HOSTS = [
    "localhost",
    "devops-app-01",
    "devops-db-01",
    "devops-worker-01",
]

# Linux-focused, predefined commands only.
DIAGNOSTIC_COMMANDS = {
    "uptime": {
        "label": "Uptime",
        "command": "uptime",
        "arg_name": None,
    },
    "disk": {
        "label": "Disk Usage",
        "command": "df -h",
        "arg_name": None,
    },
    "memory": {
        "label": "Memory Usage",
        "command": "free -m",
        "arg_name": None,
    },
    "listeners": {
        "label": "Network Listeners",
        "command": "ss -tulpn | head -n 25",
        "arg_name": None,
    },
    "service_status": {
        "label": "Systemd Service Status",
        "command": "systemctl status {service} --no-pager -n 40",
        "arg_name": "service",
    },
    "journal_filter": {
        "label": "Journal Filter",
        "command": "journalctl -n 300 --no-pager | grep -i {filter}",
        "arg_name": "filter",
    },
}

app = Flask(__name__)
app.secret_key = "devops-helper-internal"
action_logger = logging.getLogger("devops_helper_actions")
if not action_logger.handlers:
    file_handler = logging.FileHandler(ACTION_LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    action_logger.addHandler(file_handler)
    action_logger.setLevel(logging.INFO)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS deployment_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                environment TEXT NOT NULL,
                service TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        if conn.execute("SELECT COUNT(*) FROM deployment_logs").fetchone()[0] == 0:
            now = datetime.now(timezone.utc).isoformat()
            seed_rows = [
                ("prod", "api", "INFO", "Release 1.18 deployed", now),
                ("staging", "worker", "WARN", "Queue depth high", now),
                ("prod", "web", "ERROR", "Healthcheck timeout on node-2", now),
                ("prod", "api", "INFO", "Temporary API key: sk_internal_12345", now),
                ("prod", "admin", "INFO", "Rotated SSH key for devops-app-01", now),
                # additional noise
                (
                    "staging",
                    "web",
                    "INFO",
                    "Deployed commit 7c12fa3 to staging-web-02",
                    now,
                ),
                (
                    "staging",
                    "api",
                    "INFO",
                    "API container restarted after config reload",
                    now,
                ),
                (
                    "staging",
                    "worker",
                    "ERROR",
                    "Job processor crashed: redis connection reset",
                    now,
                ),
                ("staging", "worker", "INFO", "Worker pool scaled to 6 instances", now),
                (
                    "staging",
                    "db",
                    "WARN",
                    "Slow query detected on staging-db-01 (1.8s)",
                    now,
                ),
                # normal production activity
                (
                    "prod",
                    "worker",
                    "INFO",
                    "Background job cleanup completed successfully",
                    now,
                ),
                (
                    "prod",
                    "web",
                    "INFO",
                    "TLS certificate renewed via automated ACME job",
                    now,
                ),
                (
                    "prod",
                    "api",
                    "WARN",
                    "Rate limit triggered for /v1/export endpoint",
                    now,
                ),
                # sensitive style entries (realistic mistakes)
                (
                    "prod",
                    "worker",
                    "ERROR",
                    "S3 upload failed using key AKIAIOSFODNN7EXAMPLE",
                    now,
                ),
                (
                    "staging",
                    "admin",
                    "INFO",
                    "Loaded env file with AWS_SECRET_ACCESS_KEY",
                    now,
                ),
                # additional operational noise
                (
                    "staging",
                    "scheduler",
                    "INFO",
                    "Nightly cron job completed: cache prune",
                    now,
                ),
                (
                    "staging",
                    "api",
                    "WARN",
                    "Deprecated endpoint /v1/legacy called by test-suite",
                    now,
                ),
                (
                    "staging",
                    "web",
                    "INFO",
                    "Asset rebuild triggered by CI pipeline",
                    now,
                ),
                # another production log
                (
                    "prod",
                    "db",
                    "WARN",
                    "Replica lag detected on prod-db-02 (4 seconds)",
                    now,
                ),
            ]
            conn.executemany(
                """
                INSERT INTO deployment_logs (environment, service, level, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                seed_rows,
            )


def run_command(command: str) -> tuple[str, str]:
    try:
        result = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        stdout = result.stdout.strip() or "(no output)"
        stderr = result.stderr.strip()
        return stdout, stderr
    except subprocess.SubprocessError as exc:
        return "", str(exc)


def run_ssh_command(host: str, remote_command: str) -> tuple[str, str]:
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=8",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                host,
                remote_command,
            ],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        stdout = result.stdout.strip() or "(no output)"
        stderr = result.stderr.strip()
        return stdout, stderr
    except subprocess.SubprocessError as exc:
        return "", str(exc)


def build_log_query(search_text: str) -> str:
    sql = "SELECT * FROM deployment_logs"

    if search_text:
        sql += (
            " WHERE "
            f"(environment LIKE '%{search_text}%' "
            f"OR service LIKE '%{search_text}%' "
            f"OR level LIKE '%{search_text}%' "
            f"OR message LIKE '%{search_text}%')"
        )

    limit = 100 if search_text else 50
    sql += f" ORDER BY id DESC LIMIT {limit}"
    return sql


def get_actor() -> str:
    return (
        request.headers.get("X-Forwarded-User")
        or request.headers.get("X-User")
        or request.remote_addr
        or "unknown"
    )


def log_action(action: str, details: str) -> None:
    action_logger.info("user=%s action=%s details=%s", get_actor(), action, details)


def build_remote_diagnostic_command(command_key: str, raw_arg: str) -> tuple[str, str]:
    spec = DIAGNOSTIC_COMMANDS.get(command_key)
    if not spec:
        return "", "Choose a valid diagnostic command."

    arg_name = spec["arg_name"]
    if arg_name:
        if not raw_arg:
            return "", f"Command requires {arg_name} input."
        return spec["command"].format(**{arg_name: raw_arg}), ""

    return spec["command"], ""


def uptime_summary() -> str:
    out, err = run_command("uptime -p")
    if out:
        return out
    if err:
        return f"Unable to read uptime: {err}"
    return "Unable to read uptime"


def read_env() -> list[tuple[str, str]]:
    if not ENV_FILE.exists():
        return []

    items: list[tuple[str, str]] = []
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        items.append((key.strip(), value.strip()))
    return items


def write_env(items: list[tuple[str, str]]) -> None:
    text = "\n".join(f"{k}={v}" for k, v in items)
    if text:
        text += "\n"
    ENV_FILE.write_text(text, encoding="utf-8")


def parse_key_values(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


@app.get("/")
def dashboard():
    query = request.args.get("q", "").strip()

    with db() as conn:
        sql = build_log_query(query)
        if query:
            logs = conn.execute(sql).fetchall()
        else:
            logs = {}

        templates = conn.execute(
            "SELECT id, name, body, created_at FROM message_templates ORDER BY id DESC"
        ).fetchall()

    return render_template(
        "index.html",
        uptime=uptime_summary(),
        logs=logs,
        query=query,
        allowed_hosts=ALLOWED_HOSTS,
        diagnostics=DIAGNOSTIC_COMMANDS,
        env_items=read_env(),
        templates=templates,
    )


@app.post("/diagnostics")
def run_diagnostics():
    host = request.form.get("host", "").strip()
    key = request.form.get("command_key", "").strip()
    command_arg = request.form.get("command_arg", "").strip()

    if host not in ALLOWED_HOSTS:
        flash("Choose a valid allowed host.", "error")
        return redirect(url_for("dashboard"))

    remote_command, build_error = build_remote_diagnostic_command(key, command_arg)
    if build_error:
        flash(build_error, "error")
        return redirect(url_for("dashboard"))

    if host == "localhost":
        output, error = run_command(remote_command)
    else:
        output, error = run_ssh_command(host, remote_command)
    log_action(
        "run_diagnostic",
        f"host={host} command_key={key} command_arg={command_arg or '-'}",
    )

    return render_template(
        "result.html",
        title=f"Diagnostic: {key} on {host}",
        action=f"ssh {host} {remote_command}",
        output=output,
        error=error,
        context="",
    )


@app.post("/env/save")
def save_env():
    key = request.form.get("key", "").strip()
    value = request.form.get("value", "").strip()

    if not key:
        flash("Key is required.", "error")
        return redirect(url_for("dashboard"))

    items = read_env()
    replaced = False
    for i, (existing_key, _) in enumerate(items):
        if existing_key == key:
            items[i] = (key, value)
            replaced = True
            break
    if not replaced:
        items.append((key, value))

    write_env(items)
    log_action("save_env", f"key={key}")
    flash(f"Saved {key}.", "success")
    return redirect(url_for("dashboard"))


@app.post("/env/delete")
def delete_env():
    key = request.form.get("key", "").strip()
    if not key:
        flash("Key is required.", "error")
        return redirect(url_for("dashboard"))

    items = [(k, v) for (k, v) in read_env() if k != key]
    write_env(items)
    log_action("delete_env", f"key={key}")
    flash(f"Deleted {key}.", "success")
    return redirect(url_for("dashboard"))


@app.post("/templates/create")
def create_template():
    name = request.form.get("name", "").strip()
    body = request.form.get("body", "").strip()

    if not name or not body:
        flash("Template name and body are required.", "error")
        return redirect(url_for("dashboard"))

    with db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO message_templates (name, body, created_at)
                VALUES (?, ?, ?)
                """,
                (name, body, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            flash("Template name already exists.", "error")
            return redirect(url_for("dashboard"))

    log_action("create_template", f"name={name}")
    flash(f"Created template '{name}'.", "success")
    return redirect(url_for("dashboard"))


@app.post("/templates/render")
def render_message():
    template_id = request.form.get("template_id", "").strip()
    raw_context = request.form.get("context", "")

    if not template_id:
        flash("Select a template.", "error")
        return redirect(url_for("dashboard"))

    with db() as conn:
        row = conn.execute(
            "SELECT id, name, body FROM message_templates WHERE id = ?", (template_id,)
        ).fetchone()

    if not row:
        flash("Template not found.", "error")
        return redirect(url_for("dashboard"))

    values = parse_key_values(raw_context)
    try:
        output = Template(row["body"]).render(**values)
        error = ""
    except TemplateError as exc:
        output = ""
        error = str(exc)

    log_action("render_template", f"name={row['name']}")
    return render_template(
        "result.html",
        title=f"Template Render: {row['name']}",
        action="Jinja2 render",
        output=output or "(no output)",
        error=error,
        context=raw_context,
    )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
