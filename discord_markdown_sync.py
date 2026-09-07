#!/usr/bin/env python3
"""Incrementally send date-prefixed Markdown files to a Discord webhook.

Required environment variables:
    MARKDOWN_SYNC_DIR       Directory containing the Markdown files.
    DISCORD_WEBHOOK_URL     Discord webhook URL.

Optional environment variables:
    MARKDOWN_SYNC_STATE_FILE  Checkpoint file (default: <sync-dir>/.discord-sync-state.json).
    DISCORD_USERNAME          Override the webhook display name.
    DISCORD_AVATAR_URL        Override the webhook avatar.
    DISCORD_REQUEST_TIMEOUT   HTTP timeout in seconds (default: 30).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DISCORD_CONTENT_LIMIT = 2_000
GENESIS_HASH = "0" * 64
MAX_ATTEMPTS = 5
DATE_PREFIXES = (
    re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?:\D|$)"),
    re.compile(r"^(?P<year>\d{4})_(?P<month>\d{2})_(?P<day>\d{2})(?:\D|$)"),
    re.compile(r"^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})(?:\D|$)"),
)


@dataclass(frozen=True)
class Config:
    source_dir: Path
    state_file: Path
    webhook_url: str
    username: str | None
    avatar_url: str | None
    timeout: float


def load_config() -> Config:
    source = os.environ.get("MARKDOWN_SYNC_DIR")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not source:
        raise ValueError("MARKDOWN_SYNC_DIR is required")
    if not webhook:
        raise ValueError("DISCORD_WEBHOOK_URL is required")
    if not webhook.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        raise ValueError("DISCORD_WEBHOOK_URL does not look like a Discord webhook URL")

    source_dir = Path(source).expanduser().resolve()
    state_value = os.environ.get("MARKDOWN_SYNC_STATE_FILE")
    state_file = (
        Path(state_value).expanduser().resolve()
        if state_value
        else source_dir / ".discord-sync-state.json"
    )
    try:
        timeout = float(os.environ.get("DISCORD_REQUEST_TIMEOUT", "30"))
    except ValueError as exc:
        raise ValueError("DISCORD_REQUEST_TIMEOUT must be a number") from exc
    if timeout <= 0:
        raise ValueError("DISCORD_REQUEST_TIMEOUT must be greater than zero")

    return Config(
        source_dir=source_dir,
        state_file=state_file,
        webhook_url=webhook,
        username=os.environ.get("DISCORD_USERNAME"),
        avatar_url=os.environ.get("DISCORD_AVATAR_URL"),
        timeout=timeout,
    )


def date_sort_key(path: Path) -> tuple[int, int, int, int, str]:
    """Sort recognized date prefixes chronologically, then by full filename."""
    for pattern in DATE_PREFIXES:
        match = pattern.match(path.name)
        if match:
            year, month, day = (int(match.group(key)) for key in ("year", "month", "day"))
            try:
                date(year, month, day)
            except ValueError:
                continue
            else:
                return (0, year, month, day, path.name.casefold())
    logging.warning("Markdown filename has no recognized date prefix: %s", path.name)
    return (1, 9999, 12, 31, path.name.casefold())


def markdown_files(source_dir: Path) -> list[Path]:
    if not source_dir.is_dir():
        raise ValueError(f"MARKDOWN_SYNC_DIR is not a directory: {source_dir}")
    return sorted(
        (path for path in source_dir.iterdir() if path.is_file() and path.suffix.lower() in {".md", ".markdown"}),
        key=date_sort_key,
    )


def chained_hash(previous_hash: str, content: bytes) -> str:
    try:
        previous = bytes.fromhex(previous_hash)
    except ValueError as exc:
        raise ValueError("checkpoint contains an invalid synced hash") from exc
    if len(previous) != 32:
        raise ValueError("checkpoint contains an invalid synced hash")
    return hashlib.sha256(previous + content).hexdigest()


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "synced_hash": GENESIS_HASH, "files": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read checkpoint {path}: {exc}") from exc
    if state.get("version") != 1 or not isinstance(state.get("files"), list):
        raise ValueError(f"unsupported or invalid checkpoint: {path}")
    return state


def save_state(path: Path, files: list[dict[str, str]], synced_hash: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "synced_hash": synced_hash,
        "files": files,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_history(
    files: list[Path], state: dict[str, Any]
) -> tuple[int, list[dict[str, str]], str]:
    """Validate the complete saved chain and return its append position.

    Previously synchronized history is immutable. Only files after the final
    verified entry may be synchronized.
    """
    old_entries = state["files"]
    if len(files) < len(old_entries):
        raise ValueError(
            "synchronized history is broken: one or more previously synced files were removed"
        )

    verified: list[dict[str, str]] = []
    previous_hash = GENESIS_HASH
    for position, entry in enumerate(old_entries):
        path = files[position]
        if not isinstance(entry, dict):
            raise ValueError(f"checkpoint entry {position + 1} is invalid")
        if entry.get("name") != path.name:
            raise ValueError(
                "synchronized history is broken at position "
                f"{position + 1}: expected {entry.get('name')!r}, found {path.name!r}"
            )
        content = path.read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()
        next_hash = chained_hash(previous_hash, content)
        if entry.get("content_hash") != content_hash:
            raise ValueError(
                f"synchronized history is broken: contents changed for {path.name!r}"
            )
        if entry.get("chain_hash") != next_hash:
            raise ValueError(
                f"synchronized history is broken: chain hash mismatch for {path.name!r}"
            )
        verified.append({"name": path.name, "content_hash": content_hash, "chain_hash": next_hash})
        previous_hash = next_hash

    saved_synced_hash = state.get("synced_hash")
    if saved_synced_hash != previous_hash:
        raise ValueError(
            "synchronized history is broken: top-level synced hash does not match the last chain link"
        )
    return len(verified), verified, previous_hash


def split_text(text: str, limit: int) -> list[str]:
    """Split on newlines/whitespace when possible, without losing any text."""
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        else:
            split_at += 1  # Preserve the delimiter exactly.
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    chunks.append(remaining)
    return chunks


def messages_for_file(filename: str, text: str) -> list[str]:
    # Reserve enough room for a filename and a part counter on every message.
    safe_name = filename.replace("`", "ˋ")
    safe_name = safe_name if len(safe_name) <= 180 else safe_name[:177] + "..."
    body_limit = DISCORD_CONTENT_LIMIT - len(f"**{safe_name}** (part 9999999999/9999999999)\n")
    bodies = split_text(text, body_limit)
    total = len(bodies)
    messages = []
    for number, body in enumerate(bodies, 1):
        heading = f"**{safe_name}**"
        if total > 1:
            heading += f" (part {number}/{total})"
        message = f"{heading}\n{body}" if body else f"{heading}\n_(empty file)_"
        if len(message) > DISCORD_CONTENT_LIMIT:
            raise AssertionError("generated Discord message exceeds 2,000 characters")
        messages.append(message)
    return messages


def retry_delay(response_headers: Any, body: bytes, attempt: int) -> float:
    try:
        data = json.loads(body.decode("utf-8"))
        if "retry_after" in data:
            return max(0.0, float(data["retry_after"]))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        return max(0.0, float(response_headers.get("Retry-After")))
    except (TypeError, ValueError):
        return min(2 ** attempt, 30)


def post_message(config: Config, content: str) -> None:
    payload: dict[str, Any] = {
        "content": content,
        "allowed_mentions": {"parse": []},
    }
    if config.username:
        payload["username"] = config.username
    if config.avatar_url:
        payload["avatar_url"] = config.avatar_url
    data = json.dumps(payload).encode("utf-8")

    for attempt in range(MAX_ATTEMPTS):
        request = Request(
            config.webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "discord-markdown-sync/1.0"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=config.timeout) as response:
                response.read()
            return
        except HTTPError as exc:
            body = exc.read()
            if exc.code != 429 and not 500 <= exc.code < 600:
                detail = body.decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Discord returned HTTP {exc.code}: {detail}") from exc
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"Discord still returned HTTP {exc.code} after {MAX_ATTEMPTS} attempts") from exc
            delay = retry_delay(exc.headers, body, attempt)
            logging.warning("Discord returned HTTP %d; retrying in %.1fs", exc.code, delay)
            time.sleep(delay)
        except URLError as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"cannot reach Discord after {MAX_ATTEMPTS} attempts: {exc.reason}") from exc
            delay = min(2 ** attempt, 30)
            logging.warning("Cannot reach Discord; retrying in %ds: %s", delay, exc.reason)
            time.sleep(delay)


def sync(config: Config) -> int:
    files = markdown_files(config.source_dir)
    state = load_state(config.state_file)
    start, entries, current_hash = validate_history(files, state)

    if start == len(files):
        logging.info("Already up to date (%d Markdown files)", len(files))
        return 0

    synced = 0
    for path in files[start:]:
        content = path.read_bytes()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Markdown file is not valid UTF-8: {path}") from exc

        messages = messages_for_file(path.name, text)
        logging.info("Syncing %s in %d Discord message(s)", path.name, len(messages))
        for message in messages:
            post_message(config, message)

        content_hash = hashlib.sha256(content).hexdigest()
        current_hash = chained_hash(current_hash, content)
        entries.append({"name": path.name, "content_hash": content_hash, "chain_hash": current_hash})
        save_state(config.state_file, entries, current_hash)
        synced += 1

    logging.info("Synced %d file(s); synced hash is %s", synced, current_hash)
    return synced


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config()
        sync(config)
    except (OSError, ValueError, RuntimeError) as exc:
        logging.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
