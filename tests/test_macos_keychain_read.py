"""Tests for the keychain reader.

Two layers, both real. The argv and exit-code handling run through an actual
`security` stub on PATH via a real subprocess, because "did we build the right
command line" is what breaks. The decoding rules run against the bytes the real
tool was measured emitting, since those are the part nobody expects.

No test touches the real keychain.
"""

from __future__ import annotations

import subprocess

import pytest

from brief.lib.macos_keychain_read import (
    NOT_FOUND_STATUS,
    KeychainError,
    is_available,
    read_secret,
    read_secrets,
)
from harness.stub_cli import stub_clis  # noqa: F401  (pytest fixture)


def fake_run(status: int, stdout: bytes = b"", stderr: bytes = b""):
    def run(argv, timeout):
        return status, stdout, stderr

    return run


# --- the command line, through a real subprocess ---------------------------


def test_the_argv_asks_for_exactly_this_service_and_account(stub_clis):
    stub_clis.install("security")
    stub_clis.configure("security", reply="a-secret")

    read_secret("topic-brief", "CD_TOKEN", security_bin=str(stub_clis.bin_dir / "security"))

    argv = stub_clis.last()["argv"]
    assert argv[0] == "find-generic-password"
    assert argv[argv.index("-s") + 1] == "topic-brief"
    assert argv[argv.index("-a") + 1] == "CD_TOKEN"
    assert "-w" in argv, "-w is what prints the password rather than the metadata"


def test_a_named_keychain_is_passed_through(stub_clis):
    stub_clis.install("security")
    stub_clis.configure("security", reply="x")
    read_secret(
        "s",
        "a",
        keychain="/tmp/other.keychain-db",
        security_bin=str(stub_clis.bin_dir / "security"),
    )
    assert "/tmp/other.keychain-db" in stub_clis.last()["argv"]


def test_no_keychain_means_the_users_search_list(stub_clis):
    stub_clis.install("security")
    stub_clis.configure("security", reply="x")
    read_secret("s", "a", security_bin=str(stub_clis.bin_dir / "security"))
    assert not any(a.endswith(".keychain-db") for a in stub_clis.last()["argv"])


def test_a_real_subprocess_returns_the_value(stub_clis):
    stub_clis.install("security")
    stub_clis.configure("security", reply="through-a-real-process")
    got = read_secret("s", "a", security_bin=str(stub_clis.bin_dir / "security"))
    assert got == "through-a-real-process"


def test_a_missing_binary_is_an_error_not_a_missing_secret():
    with pytest.raises(KeychainError) as caught:
        read_secret("s", "a", security_bin="/nonexistent/security")
    assert "not found" in str(caught.value)


# --- the three measured behaviours ----------------------------------------


def test_a_non_ascii_value_arrives_hex_encoded_and_is_decoded():
    """Measured: `tök en-with spaces` came back as these hex digits."""
    hexed = b"74c3b66b20656e2d7769746820737061636573\n"
    assert read_secret("s", "a", run=fake_run(0, hexed)) == "tök en-with spaces"


def test_a_password_that_happens_to_be_hex_survives_unchanged():
    """A failed decode means the text was already the secret, not an error."""
    assert read_secret("s", "a", run=fake_run(0, b"deadbeef\n")) == "deadbeef"
    assert (
        read_secret("s", "a", run=fake_run(0, b"0123456789abcdef\n"))
        == "0123456789abcdef"
    )


def test_odd_length_and_non_hex_text_is_left_alone():
    assert read_secret("s", "a", run=fake_run(0, b"abc\n")) == "abc"
    assert (
        read_secret("s", "a", run=fake_run(0, b"not-hex-at-all\n")) == "not-hex-at-all"
    )


def test_exactly_one_trailing_newline_is_stripped():
    assert read_secret("s", "a", run=fake_run(0, b"secret\n")) == "secret"
    assert read_secret("s", "a", run=fake_run(0, b"secret\n\n")) == "secret\n"


def test_a_trailing_space_in_the_secret_survives():
    """Stripping whitespace rather than one newline would corrupt this."""
    assert (
        read_secret("s", "a", run=fake_run(0, b"ends with a space \n"))
        == "ends with a space "
    )


def test_not_found_is_none_not_an_exception():
    assert (
        read_secret(
            "s", "a", run=fake_run(NOT_FOUND_STATUS, b"", b"could not be found")
        )
        is None
    )


def test_a_locked_keychain_raises_rather_than_reading_as_unset():
    """The distinction the whole module exists for: unreadable is not unset."""
    with pytest.raises(KeychainError) as caught:
        read_secret(
            "svc", "acct", run=fake_run(36, b"", b"SecKeychainUnlock: locked\ndetail")
        )
    message = str(caught.value)
    assert "svc" in message and "acct" in message
    assert "36" in message
    assert "locked" in message


def test_a_timeout_is_an_error_naming_the_likely_cause():
    def slow(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(KeychainError) as caught:
        read_secret("s", "a", run=slow)
    assert "timed out" in str(caught.value)


def test_an_error_message_never_carries_the_value():
    with pytest.raises(KeychainError) as caught:
        read_secret("s", "a", run=fake_run(1, b"THE-SECRET-VALUE\n", b"some failure"))
    assert "THE-SECRET-VALUE" not in str(caught.value)


# --- the mapping form -----------------------------------------------------


def test_read_secrets_omits_the_accounts_that_do_not_exist():
    def run(argv, timeout):
        account = argv[argv.index("-a") + 1]
        return (
            (0, b"value\n", b"")
            if account == "PRESENT"
            else (NOT_FOUND_STATUS, b"", b"")
        )

    got = read_secrets("s", ["PRESENT", "ABSENT"], run=run)
    assert got == {"PRESENT": "value"}
    assert "ABSENT" not in got, "omitted, not None, so it can be merged over defaults"


def test_read_secrets_with_no_accounts_is_empty():
    assert read_secrets("s", [], run=fake_run(0, b"x\n")) == {}


def test_one_unreadable_account_fails_the_whole_call():
    """A partial credential set that looks complete is the dangerous outcome."""

    def run(argv, timeout):
        account = argv[argv.index("-a") + 1]
        return (0, b"v\n", b"") if account == "OK" else (36, b"", b"locked")

    with pytest.raises(KeychainError):
        read_secrets("s", ["OK", "LOCKED"], run=run)


# --- arguments ------------------------------------------------------------


@pytest.mark.parametrize("service,account", [("", "a"), ("s", ""), ("", "")])
def test_a_blank_service_or_account_is_refused(service, account):
    with pytest.raises(ValueError):
        read_secret(service, account, run=fake_run(0, b"x\n"))


def test_is_available_reports_on_the_named_binary(stub_clis):
    stub_clis.install("security")
    assert is_available(security_bin=str(stub_clis.bin_dir / "security")) is True
    assert is_available(security_bin="/nonexistent/security") is False


def test_the_default_binary_is_absolute():
    """PATH must not be able to redirect a credential read."""
    from brief.lib.macos_keychain_read import DEFAULT_SECURITY_BIN

    assert DEFAULT_SECURITY_BIN.startswith("/")
