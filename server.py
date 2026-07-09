#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


STATUS_RE = re.compile(r"^\s*SOURCE_OF_TRUTH_DEVIATION\s*:\s*(yes|no)\s*$", re.I | re.M)
RESOLVED_RE = re.compile(r"^\s*RESOLVED_PRIOR_ISSUES\s*:\s*(.+?)\s*$", re.I | re.M)


@dataclasses.dataclass(frozen=True)
class Settings:
    host: str
    port: int
    github_webhook_secret: str
    repository_url: str
    default_branch: str
    data_dir: Path
    source_of_truth_file: str
    codex_bin: str
    codex_model: str
    codex_timeout_seconds: int
    max_prior_reports: int
    telegram_bot_token: str
    telegram_chat_id: str

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        if env_file:
            load_env_file(env_file)
        return cls(
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8080")),
            github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
            repository_url=os.getenv("REPOSITORY_URL", ""),
            default_branch=os.getenv("DEFAULT_BRANCH", "main"),
            data_dir=Path(os.getenv("DATA_DIR", "./data")).resolve(),
            source_of_truth_file=os.getenv("SOURCE_OF_TRUTH_FILE", "SOURCE_OF_TRUTH.md"),
            codex_bin=os.getenv("CODEX_BIN", "codex"),
            codex_model=os.getenv("CODEX_MODEL", ""),
            codex_timeout_seconds=int(os.getenv("CODEX_TIMEOUT_SECONDS", "900")),
            max_prior_reports=int(os.getenv("MAX_PRIOR_REPORTS", "8")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        )


@dataclasses.dataclass(frozen=True)
class PushEvent:
    delivery_id: str
    full_name: str
    clone_url: str
    ref: str
    branch: str
    before: str
    after: str
    pusher: str
    compare_url: str
    head_message: str


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def parse_push_event(payload: dict[str, Any], delivery_id: str) -> PushEvent | None:
    if payload.get("deleted") is True:
        return None

    repo = payload.get("repository") or {}
    head = payload.get("head_commit") or {}
    ref = payload.get("ref", "")
    branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref

    full_name = str(repo.get("full_name") or "").strip()
    clone_url = str(repo.get("clone_url") or "").strip()
    after = str(payload.get("after") or "").strip()
    before = str(payload.get("before") or "").strip()

    if not full_name or not clone_url or not after:
        raise ValueError("push payload is missing repository.full_name, repository.clone_url, or after")

    pusher = str((payload.get("pusher") or {}).get("name") or "").strip()
    return PushEvent(
        delivery_id=delivery_id,
        full_name=full_name,
        clone_url=clone_url,
        ref=ref,
        branch=branch,
        before=before,
        after=after,
        pusher=pusher,
        compare_url=str(payload.get("compare") or ""),
        head_message=str(head.get("message") or ""),
    )


def safe_repo_name(full_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", full_name).strip("._-") or "repo"


def timestamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=True,
    )


class ReviewAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.work_queue: queue.Queue[PushEvent] = queue.Queue()
        self.repo_locks: dict[str, threading.Lock] = {}
        self.repo_locks_guard = threading.Lock()

    def start(self) -> None:
        worker = threading.Thread(target=self._worker_loop, name="review-worker", daemon=True)
        worker.start()

    def enqueue(self, event: PushEvent) -> None:
        self.work_queue.put(event)

    def _worker_loop(self) -> None:
        while True:
            event = self.work_queue.get()
            try:
                self.process_event(event)
            except Exception as exc:
                message = f"Code review agent failed for {event.full_name}@{event.after[:8]}: {exc}"
                print(message, flush=True)
                traceback.print_exc()
                self.send_telegram(message)
            finally:
                self.work_queue.task_done()

    def process_event(self, event: PushEvent) -> None:
        lock = self._lock_for(event.full_name)
        with lock:
            delivery_path = self.delivery_marker_path(event)
            if delivery_path.exists():
                print(f"Skipping already processed delivery {event.delivery_id}", flush=True)
                return

            repo_path = self.sync_repository(event)
            general_dir = self.report_root(event) / "general"
            bug_dir = self.report_root(event) / "bugs"
            general_dir.mkdir(parents=True, exist_ok=True)
            bug_dir.mkdir(parents=True, exist_ok=True)

            prior_context = self.collect_prior_reports(event)
            review_md = self.run_codex_review(event, repo_path, prior_context)
            deviation = has_source_of_truth_deviation(review_md)
            resolved = parse_resolved_prior_issues(review_md)

            base = f"{timestamp()}__{event.after[:12]}"
            general_path = general_dir / f"{base}.md"
            general_path.write_text(review_md, encoding="utf-8")
            bug_path: Path | None = None

            message = (
                f"Review complete for {event.full_name}@{event.after[:8]}\n"
                f"Branch: {event.branch}\n"
                f"Report: {general_path.name}"
            )

            if deviation:
                bug_path = bug_dir / f"{base}.md"
                bug_path.write_text(review_md, encoding="utf-8")
                message += f"\nALERT: Source-of-truth deviation detected.\nBug report: {bug_path.name}"

            if resolved:
                message += "\nALERT: Resolved prior issue(s): " + "; ".join(resolved[:5])

            self.send_telegram(message)
            self.send_telegram_document(general_path, f"General review for {event.full_name}@{event.after[:8]}")
            if bug_path:
                self.send_telegram_document(bug_path, f"Bug report for {event.full_name}@{event.after[:8]}")

            delivery_path.parent.mkdir(parents=True, exist_ok=True)
            delivery_path.write_text(
                json.dumps(
                    {
                        "delivery_id": event.delivery_id,
                        "repository": event.full_name,
                        "commit": event.after,
                        "processed_at": dt.datetime.now(dt.UTC).isoformat(),
                        "general_report": str(general_path),
                        "bug_report": str(bug_path) if bug_path else None,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    def _lock_for(self, full_name: str) -> threading.Lock:
        key = safe_repo_name(full_name)
        with self.repo_locks_guard:
            if key not in self.repo_locks:
                self.repo_locks[key] = threading.Lock()
            return self.repo_locks[key]

    def repo_root(self, event: PushEvent) -> Path:
        return self.settings.data_dir / "repos" / safe_repo_name(event.full_name)

    def report_root(self, event: PushEvent) -> Path:
        return self.settings.data_dir / "reports" / safe_repo_name(event.full_name)

    def delivery_marker_path(self, event: PushEvent) -> Path:
        safe_delivery = re.sub(r"[^A-Za-z0-9_.-]+", "_", event.delivery_id)
        return self.settings.data_dir / "state" / safe_repo_name(event.full_name) / f"{safe_delivery}.json"

    def sync_repository(self, event: PushEvent) -> Path:
        repos_dir = self.settings.data_dir / "repos"
        repos_dir.mkdir(parents=True, exist_ok=True)
        repo_path = self.repo_root(event)
        repo_url = self.settings.repository_url or event.clone_url

        if not repo_path.exists():
            run(["git", "clone", repo_url, str(repo_path)], timeout=900)
        else:
            run(["git", "remote", "set-url", "origin", repo_url], cwd=repo_path)
            run(["git", "fetch", "--prune", "origin"], cwd=repo_path, timeout=900)

        if event.branch:
            run(
                ["git", "fetch", "origin", f"+refs/heads/{event.branch}:refs/remotes/origin/{event.branch}"],
                cwd=repo_path,
                timeout=900,
            )
        else:
            run(["git", "fetch", "origin", event.after], cwd=repo_path, timeout=900)
        run(["git", "checkout", "--force", event.after], cwd=repo_path)
        run(["git", "clean", "-fd"], cwd=repo_path)
        return repo_path

    def collect_prior_reports(self, event: PushEvent) -> str:
        root = self.report_root(event)
        paths = sorted(root.glob("*/*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        snippets: list[str] = []
        for path in paths[: self.settings.max_prior_reports]:
            text = path.read_text(encoding="utf-8", errors="replace")
            snippets.append(f"## Prior report: {path.parent.name}/{path.name}\n\n{text[:12000]}")
        return "\n\n---\n\n".join(snippets) or "No prior reports exist for this repository."

    def run_codex_review(self, event: PushEvent, repo_path: Path, prior_context: str) -> str:
        source_path = repo_path / self.settings.source_of_truth_file
        if not source_path.exists():
            raise FileNotFoundError(f"source-of-truth file not found: {self.settings.source_of_truth_file}")

        diff = self.commit_diff(repo_path, event)
        prompt = build_review_prompt(event, self.settings.source_of_truth_file, diff, prior_context)

        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as output:
            output_path = Path(output.name)

        cmd = [
            self.settings.codex_bin,
            "exec",
            "-C",
            str(repo_path),
            "-s",
            "read-only",
            "--output-last-message",
            str(output_path),
            "-",
        ]
        if self.settings.codex_model:
            cmd[2:2] = ["-m", self.settings.codex_model]

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.settings.codex_timeout_seconds,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip() or result.stdout.strip() or "no Codex output"
                raise RuntimeError(f"Codex CLI failed with exit {result.returncode}: {stderr[-4000:]}")
            review = output_path.read_text(encoding="utf-8", errors="replace").strip()
        finally:
            output_path.unlink(missing_ok=True)

        if not review:
            raise RuntimeError("Codex returned an empty review")
        return review + "\n"

    def commit_diff(self, repo_path: Path, event: PushEvent) -> str:
        zero_sha = "0" * 40
        if event.before and event.before != zero_sha:
            cmd = ["git", "diff", "--stat", f"{event.before}..{event.after}"]
            stat = run(cmd, cwd=repo_path).stdout
            diff = run(["git", "diff", "--find-renames", f"{event.before}..{event.after}"], cwd=repo_path).stdout
            return f"$ {' '.join(cmd)}\n{stat}\n\n$ git diff --find-renames {event.before}..{event.after}\n{diff}"

        stat = run(["git", "show", "--stat", "--oneline", event.after], cwd=repo_path).stdout
        diff = run(["git", "show", "--format=medium", "--find-renames", event.after], cwd=repo_path).stdout
        return f"$ git show --stat --oneline {event.after}\n{stat}\n\n$ git show --format=medium --find-renames {event.after}\n{diff}"

    def send_telegram(self, text: str) -> None:
        token = self.settings.telegram_bot_token
        chat_id = self.settings.telegram_chat_id
        if not token or not chat_id:
            print("Telegram not configured; message follows:\n" + text, flush=True)
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:3900]}).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
        except urllib.error.URLError as exc:
            print(f"Telegram send failed: {exc}", flush=True)

    def send_telegram_document(self, path: Path, caption: str) -> None:
        token = self.settings.telegram_bot_token
        chat_id = self.settings.telegram_chat_id
        if not token or not chat_id:
            return

        boundary = f"----codex-review-agent-{int(time.time() * 1000)}"
        file_bytes = path.read_bytes()
        parts = [
            form_field(boundary, "chat_id", chat_id),
            form_field(boundary, "caption", caption[:1024]),
            file_field(boundary, "document", path.name, "text/markdown", file_bytes),
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(parts)
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response.read()
        except urllib.error.URLError as exc:
            print(f"Telegram document send failed for {path}: {exc}", flush=True)


def form_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def file_field(boundary: str, name: str, filename: str, content_type: str, data: bytes) -> bytes:
    safe_filename = filename.replace('"', "")
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{safe_filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    return header + data + b"\r\n"


def build_review_prompt(event: PushEvent, source_file: str, diff: str, prior_context: str) -> str:
    return f"""You are Codex performing an automated repository review.

Review the current checked-out repository at commit {event.after}.

Primary duties:
- Verify the change against `{source_file}`, the project-specific source of truth.
- Give a general code review of the pushed change.
- Check prior bug/general reports below and identify whether this commit fixes any prior issue.
- Also identify whether the commit accidentally reintroduces or worsens anything from prior reports.

Output a markdown report only. Start with this exact metadata block:

SOURCE_OF_TRUTH_DEVIATION: yes|no
RESOLVED_PRIOR_ISSUES: none|short semicolon-separated list

Then include these sections:

# Review
## Summary
## Source-of-Truth Compliance
## Findings
List findings by severity. Include file paths and line references when possible.
## Regression Check Against Prior Reports
## Suggested Follow-up

Repository event:
- Repository: {event.full_name}
- Branch: {event.branch}
- Commit: {event.after}
- Previous commit: {event.before}
- Pusher: {event.pusher}
- Compare URL: {event.compare_url}
- Head commit message: {event.head_message}

Diff for this push:

```diff
{diff[:90000]}
```

Prior reports:

{prior_context}
"""


def has_source_of_truth_deviation(markdown: str) -> bool:
    match = STATUS_RE.search(markdown)
    return bool(match and match.group(1).lower() == "yes")


def parse_resolved_prior_issues(markdown: str) -> list[str]:
    match = RESOLVED_RE.search(markdown)
    if not match:
        return []
    value = match.group(1).strip()
    if value.lower() in {"", "none", "no", "n/a"}:
        return []
    return [item.strip() for item in value.split(";") if item.strip()]


class GithubWebhookHandler(BaseHTTPRequestHandler):
    agent: ReviewAgent
    settings: Settings

    def do_GET(self) -> None:
        if self.path == "/health":
            self.respond_json(HTTPStatus.OK, {"ok": True})
            return
        self.respond_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/webhook/github":
            self.respond_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        signature = self.headers.get("X-Hub-Signature-256")

        if not verify_github_signature(self.settings.github_webhook_secret, body, signature):
            self.respond_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid signature"})
            return

        event_type = self.headers.get("X-GitHub-Event", "")
        if event_type != "push":
            self.respond_json(HTTPStatus.ACCEPTED, {"ignored": event_type})
            return

        delivery_id = self.headers.get("X-GitHub-Delivery", f"manual-{int(time.time())}")
        try:
            payload = json.loads(body.decode("utf-8"))
            event = parse_push_event(payload, delivery_id)
        except Exception as exc:
            self.respond_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if event is None:
            self.respond_json(HTTPStatus.ACCEPTED, {"ignored": "deleted ref"})
            return

        self.agent.enqueue(event)
        self.respond_json(HTTPStatus.ACCEPTED, {"queued": True, "delivery_id": delivery_id})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def respond_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_server(settings: Settings) -> ThreadingHTTPServer:
    agent = ReviewAgent(settings)
    agent.start()
    GithubWebhookHandler.agent = agent
    GithubWebhookHandler.settings = settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return ThreadingHTTPServer((settings.host, settings.port), GithubWebhookHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub webhook driven Codex code review agent")
    parser.add_argument("--env-file", default=".env", help="path to env file")
    args = parser.parse_args()

    settings = Settings.load(Path(args.env_file))
    if settings.github_webhook_secret == "":
        print("WARNING: GITHUB_WEBHOOK_SECRET is empty; webhook signatures will not be enforced.", flush=True)
    if shutil.which(settings.codex_bin) is None and not Path(settings.codex_bin).exists():
        raise SystemExit(f"Codex executable not found: {settings.codex_bin}")

    server = build_server(settings)
    print(f"Listening on http://{settings.host}:{settings.port}/webhook/github", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
