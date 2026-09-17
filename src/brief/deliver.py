"""Archive the brief and send it, once.

App code. Two things happen here and the order matters: the Markdown file is
written first, then the email goes out. If SMTP fails, the brief already
exists on disk and `--resend` fixes it. If the order were reversed, a send
followed by a write failure would leave a page in someone's inbox that the
archive has no record of.

Idempotency is the whole contract of this module. A `(profile, week)` whose
`sent_at` is set is refused unless the caller passes `resend=True`. Cron runs
this every Monday and a rerun after a partial failure must not produce a
second copy in a chair's inbox.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from brief.lib.markdown_email import send_markdown_email
from brief.models import Profile


class AlreadySent(RuntimeError):
    """This profile's week has been delivered. Pass `resend=True` to override."""


@dataclass(frozen=True, slots=True)
class Delivered:
    path: Path
    recipients: tuple[str, ...]
    subject: str
    refused: dict[str, Any]
    resent: bool


_SAFE_COMPONENT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class UnsafeArchiveName(ValueError):
    """A profile name or week would escape the archive directory."""


def _safe_component(value: str, field: str) -> str:
    """Refuse anything that is not a plain filename.

    Both values are operator-supplied — a profile's `name` from YAML, a week
    from `--week` — and both become path segments. A name of `../../etc` would
    write outside the archive entirely. Whitelisting is the check that does not
    need to anticipate every escape.
    """
    if not _SAFE_COMPONENT.match(value or ""):
        raise UnsafeArchiveName(
            f"{field} {value!r} is not usable as a file name; it must start with a "
            f"letter or digit and contain only letters, digits, dot, dash or underscore"
        )
    return value


def archive_path(root: str | Path, profile_name: str, week_start: str) -> Path:
    """`<root>/<profile>/<week>.md`, with both components validated."""
    return (
        Path(root)
        / _safe_component(profile_name, "profile name")
        / f"{_safe_component(week_start, 'week')}.md"
    )


def write_archive(
    markdown: str, root: str | Path, profile_name: str, week_start: str
) -> Path:
    """Write `briefs/<profile>/<week>.md`, creating directories as needed."""
    path = archive_path(root, profile_name, week_start)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def subject_for(profile: Profile, *, week_start: str, item_count: int) -> str:
    """Expand the profile's subject template.

    Only the three documented placeholders are substituted, by replacement
    rather than `str.format`, so a stray brace in a title cannot raise.
    """
    subject = profile.delivery.subject
    for placeholder, value in (
        ("{title}", profile.title),
        ("{week_start}", week_start),
        ("{n}", str(item_count)),
    ):
        subject = subject.replace(placeholder, value)
    return subject


def deliver(
    markdown: str,
    profile: Profile,
    *,
    week_start: str,
    item_count: int,
    briefs_root: str | Path,
    smtp: dict[str, Any],
    sent_at: str | None,
    resend: bool = False,
    now: datetime | None = None,
    send: Any = None,
) -> Delivered:
    """Archive then send. Refuses an already-sent week unless `resend`.

    `send` exists as a seam so a test can drive the whole path against a stub
    SMTP server, or assert that nothing was sent at all.
    """
    if sent_at and not resend:
        raise AlreadySent(
            f"{profile.name} week of {week_start} was delivered at {sent_at}; "
            f"pass --resend to send it again"
        )

    path = write_archive(markdown, briefs_root, profile.name, week_start)
    subject = subject_for(profile, week_start=week_start, item_count=item_count)

    sender = send or send_markdown_email
    refused = sender(
        markdown,
        subject=subject,
        sender=profile.delivery.sender,
        recipients=list(profile.delivery.to),
        host=str(smtp.get("host", "localhost")),
        port=int(smtp.get("port", 25)),
        timeout=float(smtp.get("timeout_s", 30.0)),
        starttls=bool(smtp.get("starttls", False)),
        username=smtp.get("username"),
        password=smtp.get("password"),
        date=now,
    )

    return Delivered(
        path=path,
        recipients=profile.delivery.to,
        subject=subject,
        refused=dict(refused or {}),
        resent=bool(sent_at),
    )
