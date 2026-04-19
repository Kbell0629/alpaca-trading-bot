"""
per_user_isolation.py — helpers that enforce per-user file isolation.

Extracted from server.py in round-15 so the critical invariant (only
user_id==1 may fall back to the shared DATA_DIR copy) can be unit-tested
without dragging in sqlite / auth / network init.

SECURITY NOTE: the user_id==1 check is load-bearing. Any regression here
cross-contaminates strategies and auto-trades on other users' Alpaca
accounts. See tests/test_round15_audit_fixes.py for the pinning tests.
"""
from __future__ import annotations

import json
import os
import shutil


def _load_json(path):
    """Best-effort JSON load. Returns None on any failure."""
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_with_shared_fallback(user_path, shared_path, user_id,
                              capture_exc=None):
    """Load a JSON overlay from the per-user path.

    CRITICAL MIGRATION RULE: only user_id==1 (bootstrap admin) may fall
    back to the shared DATA_DIR copy. OTHER users must never inherit
    shared files, or they'd get the admin's active strategies /
    guardrails / auto-deployer config and start auto-trading on their
    own Alpaca account the instant they sign up.

    `capture_exc` is an optional Sentry-style hook: a callable(exc, **kw)
    invoked if the shared→user copy fails. Defaults to None so the
    function can be used from test contexts without Sentry wired up.
    """
    val = _load_json(user_path)
    if val is not None:
        return val
    if user_id == 1:
        val = _load_json(shared_path)
        if val is not None:
            try:
                os.makedirs(os.path.dirname(user_path) or ".", exist_ok=True)
                shutil.copy2(shared_path, user_path)
            except Exception as e:
                if capture_exc is not None:
                    try:
                        capture_exc(
                            e,
                            source="load_with_shared_fallback.copy",
                            user_path=user_path,
                        )
                    except Exception:
                        pass
        return val
    return None
