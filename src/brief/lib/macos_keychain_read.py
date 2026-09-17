"""Read one secret out of the macOS keychain.

Situation: a scheduled job on your own machine needs a token, and a dotenv
file next to the code is the thing you are trying to avoid. The keychain
already holds credentials for this user, it is unlocked for the length of
a login session, and ``security`` can read it without a prompt for items
it created.

Contract: ``read_secret(service, account)`` returns the secret, or None
when there is no such item. None is an answer, not a failure — a caller
asking "is this configured?" should not need a try block. Everything
else, including a locked keychain and a denied ACL, raises
``KeychainError``, because a credential that could not be read is not the
same as one that was never set, and collapsing the two makes a job run
on without its token.

Three details this encodes, each measured rather than assumed:

* ``security -w`` prints raw HEX when the value is not printable ASCII.
  A password of ``tök en`` comes back as hex digits. Handed straight to
  an HTTP header that is a baffling 401, so an even-length all-hex line
  is decoded as UTF-8 — and falls back to the literal text when that
  fails, since a real password may be hex characters.
* Exit status 44 is "no such item". Every other non-zero status is a
  genuine failure and is raised.
* ``-w`` appends exactly one newline. One is stripped, not all trailing
  whitespace, because a secret may end in a space.

``security_bin`` defaults to an absolute path on purpose: a credential
read must not be redirectable by PATH. The ``run`` seam is for tests,
which drive a stub binary through a real subprocess.

Deliberately not here: writing secrets — that is an interactive act an
operator performs once at a shell, and a library that can silently write
credentials asks for more trust than one that only reads. Also no
caching, no retry, no environment-variable knowledge, and no notion of
what the secret is for.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable

DEFAULT_SECURITY_BIN = "/usr/bin/security"

#: ``security`` exits with this when the item simply is not there.
NOT_FOUND_STATUS = 44

Runner = Callable[[list[str], float], "tuple[int, bytes, bytes]"]


class KeychainError(RuntimeError):
    """The keychain could not be read. Names only; never a secret value."""


def is_available(*, security_bin: str = DEFAULT_SECURITY_BIN) -> bool:
    """Is this a machine with the ``security`` tool present and executable?

    A plain predicate so a caller can choose a different source rather than
    catching an exception to discover it is not on macOS.
    """
    return os.path.isfile(security_bin) and os.access(security_bin, os.X_OK)


def read_secret(
    service: str,
    account: str,
    *,
    keychain: str | None = None,
    security_bin: str = DEFAULT_SECURITY_BIN,
    timeout_s: float = 10.0,
    run: Runner | None = None,
) -> str | None:
    """The secret stored under ``service``/``account``, or None if absent."""
    if not service or not account:
        raise ValueError("service and account are both required")

    argv = [security_bin, "find-generic-password", "-s", service, "-a", account, "-w"]
    if keychain:
        argv.append(keychain)

    runner = run or _run
    try:
        status, stdout, stderr = runner(argv, timeout_s)
    except FileNotFoundError as exc:
        raise KeychainError(
            f"{security_bin} not found; this looks like a machine without the macOS "
            f"keychain tool"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise KeychainError(
            f"reading {service}/{account} timed out after {timeout_s}s; a locked "
            f"keychain waiting on a prompt looks like this"
        ) from exc

    if status == NOT_FOUND_STATUS:
        return None
    if status != 0:
        raise KeychainError(
            f"could not read {service}/{account} (exit {status}): "
            f"{_first_line(stderr) or 'no detail'}"
        )
    return _decode(stdout)


def read_secrets(
    service: str,
    accounts: Iterable[str],
    *,
    keychain: str | None = None,
    security_bin: str = DEFAULT_SECURITY_BIN,
    timeout_s: float = 10.0,
    run: Runner | None = None,
) -> dict[str, str]:
    """Every named account that exists, as a mapping. Absent ones are omitted.

    Omitted rather than mapped to None so a caller can merge the result over
    its defaults without writing a null into a config it will later read.
    """
    found: dict[str, str] = {}
    for account in accounts:
        value = read_secret(
            service,
            account,
            keychain=keychain,
            security_bin=security_bin,
            timeout_s=timeout_s,
            run=run,
        )
        if value is not None:
            found[account] = value
    return found


def _run(argv: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
    completed = subprocess.run(
        argv,
        capture_output=True,
        timeout=timeout_s,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _decode(stdout: bytes) -> str:
    """The secret as text, undoing the hex form ``security`` uses for non-ASCII."""
    text = stdout.decode("utf-8", errors="replace")
    if text.endswith("\n"):
        text = text[:-1]  # exactly one; a secret may end in a space
    return _maybe_unhex(text)


def _maybe_unhex(text: str) -> str:
    """Decode an all-hex line, or return it unchanged.

    A real password can consist only of hex characters, so a failed decode is
    not an error — it means the text was already the secret.
    """
    stripped = text.strip()
    if len(stripped) < 2 or len(stripped) % 2 or not _is_hex(stripped):
        return text
    try:
        return bytes.fromhex(stripped).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return text


def _is_hex(text: str) -> bool:
    return all(c in "0123456789abcdefABCDEF" for c in text)


def _first_line(stderr: bytes) -> str:
    """The first line of stderr, for an error message. Never a secret."""
    lines = stderr.decode("utf-8", errors="replace").strip().splitlines()
    return lines[0] if lines else ""
