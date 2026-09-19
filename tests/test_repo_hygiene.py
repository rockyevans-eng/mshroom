"""Public-repo hygiene guard: fail CI if a tracked file leaks something that
must never be published.

MSHroom is a public repository, and its authors also work on private
systems. This test scans every git-tracked text file and fails on:

* an IPv4 address other than ``127.0.0.1`` / ``0.0.0.0`` (real network
  layouts have leaked through examples and comments before),
* internal project, machine, or employer names (the term list is assembled
  from fragments below so this file does not itself contain them),
* SSN-shaped numbers (``ddd-dd-dddd``) and US-phone-shaped numbers, which
  look like patient/person data even when they are made up. Use obviously
  synthetic identifiers instead (``MSHROOM-TEST-0001``).

Every failure names ``path:line`` so the offender can be found in seconds.
The test scans *tracked* files (``git ls-files``), i.e. exactly what a push
would publish; untracked scratch files are not its business (they are
covered by ``.gitignore`` and ``SECURITY.md``).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve()

# The project's public GitHub username appears in badge/advisory URLs on
# purpose. Lines carrying it are exempt from every rule (a URL there could
# never be a leak, and the exemption keeps the rule set simple).
_PUBLIC_USERNAME = "rockyevans-eng"

# Internal names, built from fragments so the literal words never appear in
# this file (a plain-text grep of the repo for them must come back clean).
# Substring match, case-insensitive: they also must not hide inside a path
# or an identifier.
_FORBIDDEN_TERMS = [
    "work" + "horse",
    "dol" + "bey",
    "nib" + "bler",
    "glad" + "os",
    "toll" + "booth",
    "mirth" + "-lab",
    "mirth" + "lab",
]
_FORBIDDEN_TERMS_RE = re.compile("|".join(re.escape(t) for t in _FORBIDDEN_TERMS), re.IGNORECASE)

# A dotted quad that is not part of something longer: not preceded by a word
# char or dot, and not followed by a word char or ".<digit>" -- so
# "1.2.3.4.5" (a version) and "v1.2.3.4" are not addresses. Octets are
# range-checked in code (<= 255), since a regex for that is unreadable.
_IPV4_RE = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w]|\.\d)")
_ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}

# Dependency manifests are full of version numbers. In these files a dotted
# quad directly after a version operator is a version, not an address.
_MANIFEST_NAMES = ("pyproject.toml", "package-lock.json", "poetry.lock", "uv.lock", "Pipfile.lock")
_VERSION_PREFIX_RE = re.compile(r"(===|==|>=|<=|~=|!=|<|>|\^|~|v|version\s*=\s*\"?)\s*$", re.IGNORECASE)

_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)\(?\d{3}\)?[ -]\d{3}-\d{4}(?!\d)")


def _is_manifest(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name in _MANIFEST_NAMES or name.endswith(".lock") or name.startswith("requirements")


def scan_text(path: str, text: str) -> list[str]:
    """Return one ``path:line: reason`` string per violation in *text*.

    Pure function (no filesystem/git) so the rules themselves can be tested
    with synthetic input below.
    """
    problems: list[str] = []
    manifest = _is_manifest(path)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _PUBLIC_USERNAME in line:
            continue

        for m in _IPV4_RE.finditer(line):
            octets = [int(g) for g in m.groups()]
            if any(o > 255 for o in octets) or m.group(0) in _ALLOWED_IPS:
                continue  # not a real address, or one of the two allowed
            if manifest and _VERSION_PREFIX_RE.search(line[: m.start()]):
                continue
            problems.append(f"{path}:{lineno}: IPv4 address {m.group(0)} (only 127.0.0.1 / 0.0.0.0 allowed)")

        for m in _FORBIDDEN_TERMS_RE.finditer(line):
            problems.append(f"{path}:{lineno}: internal name {m.group(0)!r} must not appear in the public repo")

        for m in _SSN_RE.finditer(line):
            problems.append(f"{path}:{lineno}: SSN-shaped number {m.group(0)}")

        for m in _PHONE_RE.finditer(line):
            problems.append(f"{path}:{lineno}: US-phone-shaped number {m.group(0)}")
    return problems


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout (or git unavailable): nothing to scan")
    return [p for p in out.decode("utf-8").split("\0") if p]


def _read_text_or_none(path: Path) -> str | None:
    """File contents as text, or ``None`` for binary/unreadable files.

    A NUL byte in the first 8 KiB is the usual "this is binary" test (it is
    what git itself uses); such files (probe captures, images) are skipped.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def test_tracked_files_are_clean():
    problems: list[str] = []
    scanned = 0
    for rel in _tracked_files():
        path = REPO_ROOT / rel
        if path.resolve() == THIS_FILE:
            continue  # the rules' own test data would trip the rules
        text = _read_text_or_none(path)
        if text is None:
            continue
        scanned += 1
        problems.extend(scan_text(rel, text))
    assert scanned > 10, "scanned suspiciously few files; is git ls-files returning the tree?"
    assert not problems, "public-repo hygiene violations:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# The rules themselves, on synthetic input (so a broken regex can't make the
# repo scan pass vacuously).
# ---------------------------------------------------------------------------


def _private_ip() -> str:
    return ".".join(["192", "168", "1", "10"])  # built dynamically: no literal in this file


def test_flags_non_loopback_ipv4_with_file_and_line():
    problems = scan_text("docs/x.md", f"ok line\nhost is {_private_ip()} here\n")
    assert len(problems) == 1
    assert problems[0].startswith("docs/x.md:2:")
    assert _private_ip() in problems[0]


def test_allows_loopback_and_any_address_and_versions():
    text = "bind 127.0.0.1 or 0.0.0.0; also 1.2.3.4.5 and v1.2.3.4 and 999.1.1.1"
    assert scan_text("README.md", text) == []


def test_manifest_version_quads_are_not_addresses():
    assert scan_text("pyproject.toml", 'foo>=1.2.3.4\nbar==10.20.30.40\nversion = "5.6.7.8"') == []
    # ...but a bare address in a manifest is still flagged.
    assert scan_text("pyproject.toml", f"index = {_private_ip()}")


def test_flags_ssn_and_phone_shapes():
    ssn = "-".join(["123", "45", "6789"])
    phone = "(" + "555" + ") " + "123" + "-" + "4567"
    dashed = "-".join(["555", "123", "4567"])
    assert len(scan_text("a.txt", f"id {ssn}")) == 1
    assert len(scan_text("a.txt", f"call {phone}")) == 1
    assert len(scan_text("a.txt", f"call {dashed}")) == 1
    assert scan_text("a.txt", "MSHROOM-TEST-0001 and 20260101-120000") == []


@pytest.mark.parametrize("term", _FORBIDDEN_TERMS)
def test_flags_each_forbidden_term_case_insensitively(term):
    problems = scan_text("a.txt", f"see the {term.upper()} repo")
    assert len(problems) == 1 and "a.txt:1:" in problems[0]


def test_public_username_line_is_exempt():
    line = f"[![CI](https://github.com/{_PUBLIC_USERNAME}/mshroom/actions/workflows/ci.yml/badge.svg)]"
    assert scan_text("README.md", line) == []
