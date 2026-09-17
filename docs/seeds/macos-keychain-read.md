# Seed boundary: macos-keychain-read

Planted 2026-09-17. Second demand signal: `~/AI/utility/ClaudeUsageBar` names
`macos-keychain-read` as a sibling seed candidate in
`Sources/ClaudeUsageBarApp/KeychainCredentialSource.swift`, having implemented the same
read in Swift. This is the Python half of the same capability.

## Purpose
Read one generic-password secret out of the macOS keychain, returning `None` when there is
no such item.

## when_to_use (draft for FEATURE.toml)
Your program needs a token on a machine where you would rather not leave it in a dotenv
file — a scheduled job on your own laptop, a personal tool — and you want "no such secret"
to be an ordinary answer rather than a crash.

## Inputs
- `service: str` — the generic-password service name, e.g. `"topic-brief"`
- `account: str` — the account within it, e.g. `"CD_TOKEN"`
- `keychain: str | None = None` — a specific keychain file; `None` means the user's search list
- `security_bin: str = "/usr/bin/security"` — absolute by default, so `PATH` cannot redirect a credential read
- `timeout_s: float = 10.0`
- `run: Callable[[list[str], float], tuple[int, bytes, bytes]] | None = None` — subprocess seam, `(argv, timeout) -> (returncode, stdout, stderr)`

## Outputs
- `str` — the secret
- `None` — no such item (exit status 44), which is a normal answer, not an error
- raises `KeychainError` (subclass of `RuntimeError`) on anything else: a locked keychain, a denied ACL, a missing `security` binary, a timeout. The message carries the service and account **names only**, never a value.

## Behaviour this exists to encode

Three things that are not obvious and that a hand-rolled call gets wrong:

- **A value that is not printable ASCII comes back hex-encoded.** `security ... -w` prints
  raw hex digits when the password contains bytes it will not print. Measured:
  `tök en-with spaces` was returned as `74c3b66b20656e2d7769746820737061636573`. A naive
  reader hands that hex string to an HTTP header and gets a puzzling 401. Detect an
  even-length all-hex line and decode it as UTF-8; if decoding fails, treat the literal
  text as the secret, because a genuine password of only hex characters is possible.
- **Exit 44 means "not found", not "failed".** Every other non-zero status is a real
  failure and must not be flattened into `None`, or a locked keychain reads as an unset
  secret and the caller silently proceeds without a credential.
- **Trailing newline.** `-w` appends one. Strip exactly one trailing newline, not
  whitespace: a secret may legitimately end in a space.

## Must NOT know about
- which environment variables exist, or that environment variables are involved at all
- config files, YAML, profiles, or any application key name
- that the caller is a news pipeline; the words "brief", "profile" and "source" do not appear
- how to *write* a secret. Writing is an interactive, consenting act an operator does once at a shell; a library that can silently write credentials is a bigger thing to trust than one that can only read.
- caching or retry policy — a caller reading on a timer owns that decision

## Public API
```python
class KeychainError(RuntimeError): ...

def read_secret(service: str, account: str, *, keychain: str | None = None,
                security_bin: str = "/usr/bin/security", timeout_s: float = 10.0,
                run: Callable[[list[str], float], tuple[int, bytes, bytes]] | None = None
                ) -> str | None

def read_secrets(service: str, accounts: Iterable[str], **kw) -> dict[str, str]
    # only the accounts that exist; absent ones are simply missing from the mapping

def is_available(*, security_bin: str = "/usr/bin/security") -> bool
    # is this even a macOS machine with the tool present
```

## Test harness
stub_cli — the module shells out, so the tests install a stub `security` on PATH and drive
argv, exit codes and stdout through a real subprocess. `security_bin` is passed as the stub's
path; no test touches the real keychain.

## Approved module docstring (write this verbatim into the module)
```
Read one secret out of the macOS keychain.

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
```

## Prior art
verdict: seed

`lib_search.py` run for three phrasings. The nearest hit is `desktop-settings-keychain`
[tested], which covers the same ground through the `keyring` package plus a settings file
and `push_secrets_to_env`. It was considered and not used here: it brings a settings-file
model this project does not need (config is YAML), and `keyring` reads the keychain from
the *Python* binary's identity, which is a different ACL from `security` and prompts on a
machine where `security` does not. This seed is the no-dependency, no-prompt half. Nothing
is copied from it.

## Boundary fixes applied at write time
- `security_bin` absolute by default; never resolved through PATH.
- Error messages carry names, never values, and never the stderr of a successful read.
- `read_secrets` returns only what exists, so a caller can merge it over defaults without
  writing `None` into a config.
