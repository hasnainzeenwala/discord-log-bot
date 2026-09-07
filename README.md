# Discord Markdown Sync

Incrementally sends date-prefixed Markdown files to Discord through a webhook.
The checkpoint is an append-only SHA-256 hash chain: previously synchronized
history must remain unchanged, while new files may be appended and synchronized.

## Features

- Chronological processing of date-prefixed Markdown files
- Append-only SHA-256 history validation
- Durable, atomic checkpoints after each delivered file
- Automatic splitting at Discord's 2,000-character message limit
- Rate-limit and transient-error retries with backoff
- Suppressed `@everyone`, `@here`, role, and user mentions
- Twice-daily scheduling through a systemd user timer
- No third-party Python dependencies

## Requirements

- Linux with Python 3.10 or newer
- systemd user services
- A Discord webhook URL

The script uses only the Python standard library.

## Installation

From this repository, run:

```bash
install -Dm755 discord_markdown_sync.py \
  "$HOME/.local/bin/discord-markdown-sync"

install -Dm644 discord-markdown-sync.service \
  "$HOME/.config/systemd/user/discord-markdown-sync.service"

install -Dm644 discord-markdown-sync.timer \
  "$HOME/.config/systemd/user/discord-markdown-sync.timer"

install -Dm600 discord-markdown-sync.env.example \
  "$HOME/.config/discord-markdown-sync.env"
```

Edit the environment file:

```bash
"${EDITOR:-nano}" "$HOME/.config/discord-markdown-sync.env"
```

At minimum, set:

```dotenv
MARKDOWN_SYNC_DIR=/absolute/path/to/markdown-files
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN
```

Available settings:

| Variable | Required | Description |
| --- | --- | --- |
| `MARKDOWN_SYNC_DIR` | Yes | Directory containing `.md` and `.markdown` files |
| `DISCORD_WEBHOOK_URL` | Yes | Discord webhook endpoint |
| `MARKDOWN_SYNC_STATE_FILE` | No | Override the checkpoint location |
| `DISCORD_USERNAME` | No | Override the webhook display name |
| `DISCORD_AVATAR_URL` | No | Override the webhook avatar |
| `DISCORD_REQUEST_TIMEOUT` | No | HTTP timeout in seconds; defaults to `30` |

Load and enable the timer:

```bash
systemctl --user daemon-reload
systemctl --user enable --now discord-markdown-sync.timer
```

For a server that must run user timers while the account is logged out:

```bash
sudo loginctl enable-linger "$USER"
```

## Verification and logs

Run a synchronization immediately and inspect its logs:

```bash
systemctl --user start discord-markdown-sync.service
journalctl --user -u discord-markdown-sync.service -n 100 --no-pager
systemctl --user list-timers discord-markdown-sync.timer
```

The timer runs around midnight and noon in the machine's local timezone. A
random delay of up to five minutes prevents every installation from contacting
Discord at exactly the same instant.

## How synchronization works

On every run, the program:

1. Finds Markdown files directly inside `MARKDOWN_SYNC_DIR`.
2. Sorts them by a recognized date prefix and then by filename.
3. Rebuilds and validates every link in the saved hash chain.
4. Aborts before delivery if previously synchronized history has diverged.
5. Sends only files appended after the verified final link.
6. Atomically advances the checkpoint after each fully delivered file.

The chain begins with a 32-byte zero value and advances as follows:

```text
chain[n] = SHA256(chain[n-1] + file[n].contents)
```

## Filename order

Recognized date prefixes are:

```text
YYYY-MM-DD
YYYY_MM_DD
YYYYMMDD
```

Recognized files are sorted chronologically. Files without a valid date prefix
are sorted alphabetically after the dated files.

## Checkpoint and history rules

Under the included systemd service, the checkpoint is stored at:

```text
~/.local/state/discord-markdown-sync/state.json
```

The run aborts before sending anything when an already synchronized file was
modified, removed, renamed, reordered, or when another file was inserted into
the synchronized sequence. Only files appended after the verified final link
are sent.

Each file is split into messages of at most 2,000 characters. The checkpoint
advances after every completely delivered file. If delivery fails partway
through a file, the entire file is attempted again on the next run. Delivery is
therefore at least once: a partial failure can cause already accepted chunks of
that file to appear again.

The checkpoint and the real environment file are intentionally excluded from
Git. Keep the webhook URL secret; anyone possessing it can post to its channel.

## rclone dependency

The included service requires `rclone-hasnain-drive.service` and waits for that
user service to signal readiness before synchronizing. Change or remove these
two lines if your Markdown directory is not provided by that mount:

```ini
Requires=rclone-hasnain-drive.service
After=rclone-hasnain-drive.service
```

Because this is a user-level dependency, the rclone unit must also be installed
as a user service.
