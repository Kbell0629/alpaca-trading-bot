"""Round-61 pt.22 — custom lint: ban silent `except Exception: pass`
on auth + trading code paths.

Ruff's E722 catches bare `except:` but not `except Exception:`. The
cryptography-import bug in pt.13 was caused by exactly this pattern —
a bare `except Exception` that swallowed a PanicException (which is
BaseException, not Exception, but the bug-compat fallback to
`except Exception` made the failure invisible for hours). This test
enforces that the auth + trading modules use explicit exception types
OR log the exception text.

Allowed patterns:
  - `except (OSError, ValueError)`                  # specific tuple
  - `except sqlite3.OperationalError`               # named class
  - `except Exception as e: log/print/raise/capture_exception(e)` # used

Disallowed:
  - `except Exception: pass`                 # silent swallow
  - `except Exception:\\n    pass`           # multiline silent
  - `except BaseException: pass`             # even worse

Files audited (defined in `_AUDITED_FILES`): anything on the
authentication, credential, or order-placement path where swallowing
exceptions could silently lose money or leak credentials.

To add a file to the audit set: append it to `_AUDITED_FILES`.
To whitelist a specific line (e.g. a legitimately best-effort
cleanup): add the marker comment `# noqa: silent-except` on the
except line — the test will skip it with a warning.
"""
from __future__ import annotations

import re


# Files where `except Exception: pass` is banned without explicit
# allow-list marker. Trading + auth + credential paths.
_AUDITED_FILES = [
    "auth.py",
    "smart_orders.py",
    "cloud_scheduler.py",
    "handlers/auth_mixin.py",
    "handlers/strategy_mixin.py",
    "handlers/actions_mixin.py",
    "error_recovery.py",
    "wheel_strategy.py",
    "scheduler_api.py",
]


_ALLOW_MARKER = "noqa: silent-except"


def _scan_file(path):
    """Return list of (line_no, snippet) offenders in `path`."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return []

    offenders = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Allow-marker lets us explicitly exempt a line.
        if _ALLOW_MARKER in line:
            continue
        # Match `except Exception:` or `except BaseException:` —
        # with optional `as e` and optional trailing comment.
        m = re.match(r"^\s*except\s+(Exception|BaseException)(?:\s+as\s+\w+)?\s*:\s*(#.*)?$", line)
        if not m:
            continue
        # Look at the next non-blank, non-comment line. If it's `pass`
        # or `continue` with no logging in between, flag it.
        next_lines = []
        for j in range(i + 1, min(i + 6, len(lines))):
            nxt = lines[j]
            # Strip leading whitespace — we only care about content.
            nxt_strip = nxt.strip()
            if not nxt_strip or nxt_strip.startswith("#"):
                continue
            next_lines.append(nxt_strip)
            # If we see a log / print / capture / raise / toast /
            # notify_user / etc, it's not silent — allow.
            if re.search(r"\b(log|print|capture_exception|capture_message|"
                         r"raise|toast|notify_user|self\._send_error_safe|"
                         r"send_json|logger\.|logging\.)\b", nxt_strip):
                break
            # If we see `pass` or `continue` alone → silent.
            if nxt_strip in ("pass", "continue"):
                offenders.append((i + 1, stripped))
                break
            # Anything else → not silent (some other action taken),
            # let it pass without flag.
            break
    return offenders


# Ratchet baseline — the count of known silent-except patterns as of
# pt.22 commit. Any file that INCREASES above its baseline fails the
# test. Opportunistic fixes + the silent-except allow-marker comment
# (see _ALLOW_MARKER above) are encouraged (and will trip the "went
# down" assertion below so the baseline gets locked to the new lower
# number).
#
# When fixing a silent-except:
#   1. Replace `except Exception: pass` with either a specific
#      exception type OR a logged catch.
#   2. Re-run this test. It will fail with "count dropped — update
#      baseline to N". Edit the baseline below to match.
#   3. The ratchet prevents regressions forever after.
_SILENT_EXCEPT_BASELINE = {
    "auth.py": 4,
    "cloud_scheduler.py": 22,
    "error_recovery.py": 1,
    "handlers/actions_mixin.py": 2,
    "handlers/auth_mixin.py": 10,
    "handlers/strategy_mixin.py": 0,
    "scheduler_api.py": 2,
    "smart_orders.py": 3,
    "wheel_strategy.py": 6,
}


def test_no_new_silent_except_on_auth_trading_paths():
    """Ratchet-style enforcement: each audited file must NOT exceed
    its pt.22-baseline count of `except Exception: pass` patterns.
    Adding a new silent-except on any of these paths is forbidden.
    Opportunistically reducing is encouraged — if the count drops,
    the test fails too (telling you to update the baseline below).
    """
    current = {}
    errors = []
    for path in _AUDITED_FILES:
        current[path] = len(_scan_file(path))
    for path, expected in _SILENT_EXCEPT_BASELINE.items():
        got = current.get(path, 0)
        if got > expected:
            offenders = _scan_file(path)
            errors.append(
                f"  {path}: {got} silent-except patterns (baseline {expected}). "
                f"New additions forbidden — either log the exception, catch a "
                f"specific type, or add `# noqa: silent-except` with a "
                f"one-line justification. Offenders:")
            for line_no, snippet in offenders[expected:]:
                errors.append(f"    L{line_no}: {snippet}")
        elif got < expected:
            errors.append(
                f"  {path}: {got} silent-except patterns "
                f"(baseline was {expected}) — count DROPPED. Update "
                f"_SILENT_EXCEPT_BASELINE['{path}'] = {got} to lock "
                f"in the improvement.")
    if errors:
        msg = "\n".join([
            "Silent-except ratchet violation (audit/trading paths):",
            "",
            *errors,
        ])
        raise AssertionError(msg)


def test_audited_files_exist():
    """Sanity: make sure the audit list doesn't drift from actual
    filenames (so a rename doesn't silently remove a file from
    the audit without us noticing)."""
    import os
    missing = [p for p in _AUDITED_FILES if not os.path.exists(p)]
    assert not missing, (
        f"Audited files missing on disk (rename or delete?): {missing}")


def test_allow_marker_skips_a_line(tmp_path):
    """The `noqa: silent-except` marker must actually skip the line."""
    snippet = (
        "try:\n"
        "    thing()\n"
        "except Exception:  # noqa: silent-except -- intentionally\n"
        "    pass\n"
    )
    f = tmp_path / "fake.py"
    f.write_text(snippet)
    offenders = _scan_file(str(f))
    assert offenders == []


def test_silent_except_flagged_on_raw_snippet(tmp_path):
    """Smoke: the scanner catches the forbidden pattern on a fresh
    file (prevents the scan from silently passing everything via a
    regex bug)."""
    snippet = (
        "def foo():\n"
        "    try:\n"
        "        thing()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    f = tmp_path / "fake.py"
    f.write_text(snippet)
    offenders = _scan_file(str(f))
    assert len(offenders) == 1
    assert "except Exception" in offenders[0][1]


def test_logged_except_not_flagged(tmp_path):
    """A logged exception is not silent — must not flag."""
    snippet = (
        "def foo():\n"
        "    try:\n"
        "        thing()\n"
        "    except Exception as e:\n"
        "        log(f'failed: {e}')\n"
    )
    f = tmp_path / "fake.py"
    f.write_text(snippet)
    offenders = _scan_file(str(f))
    assert offenders == []
