# Code Review Agent

Webhook-driven review agent for GitHub repositories.

When GitHub sends a `push` webhook, the service:

1. verifies the webhook signature,
2. clones or pulls the repository,
3. checks out the pushed commit,
4. asks Codex to review the change against the project source-of-truth markdown file,
5. saves a general markdown review,
6. saves a bug report markdown file when the code deviates from the source of truth,
7. compares against previous reports so fixed issues can be announced, and
8. sends Telegram notifications.

## Quick Start

```bash
cp config.example.env .env
python3 server.py
```

Then expose `http://HOST:PORT/webhook/github` to GitHub and configure:

- Content type: `application/json`
- Secret: same value as `GITHUB_WEBHOOK_SECRET`
- Events: `Pushes`

For local development, a tunnel such as ngrok or Cloudflare Tunnel can forward GitHub webhooks to `localhost:8080`.

## Required Repository File

Each reviewed project should contain the configured source-of-truth file, by default:

```text
SOURCE_OF_TRUTH.md
```

Use this file to describe architecture, invariants, product requirements, security rules, forbidden patterns, and acceptance criteria. The agent treats it as the project contract.

## Output Layout

By default reports are stored under `./data`:

```text
data/
  repos/
    owner__repo/
  reports/
    owner__repo/
      general/
      bugs/
  state/
```

## Environment

See `config.example.env` for every option.

Important settings:

- `GITHUB_WEBHOOK_SECRET`: verifies GitHub webhook signatures.
- `REPOSITORY_URL`: optional clone URL override. Useful for private repositories with a deploy key or token-backed URL.
- `SOURCE_OF_TRUTH_FILE`: markdown file inside the checked-out repository.
- `CODEX_BIN`: path to the Codex executable.
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`: where alerts are sent.

## Run As A Service

Keep the process running with your preferred supervisor, for example `systemd`, `launchd`, `pm2`, or Docker. The service handles each webhook in a background thread so GitHub receives a fast `202 Accepted`.

## Security Notes

- Never commit `.env`.
- Use a GitHub webhook secret.
- Prefer deploy keys or GitHub fine-grained tokens with read-only repository access.
- Run this service in a network location reachable only by GitHub or behind a trusted reverse proxy.
- The Codex invocation runs in read-only mode for the checked-out repository.
