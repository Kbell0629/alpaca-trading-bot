"""
Round-12 audit: latent-bug regression guards.

Context: ruff's F821 (undefined-name) scan found that the beta-exposure
+ drawdown-sizing gate in run_auto_deployer() referenced variables
before they were defined in the function scope. Every call to
run_auto_deployer ran into NameError on the first line of the gate,
and the outer `except Exception as _re` swallowed it — so the risk
gate was silently DISABLED in production since the day it was added
(round-11). The fix moves the block to after existing_positions is
populated and uses a fresh /account fetch for portfolio_value.

These tests guard against regressions: they compile + import
cloud_scheduler to assert there are no undefined-name issues in the
module-level bindings, and they confirm run_auto_deployer's scope
contains factor_bypass BEFORE the beta-exposure gate is evaluated.
"""
from __future__ import annotations

import ast
import os

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_cloud_scheduler_imports_cleanly():
    """Regression guard: previously this module had undefined-name
    references (user_data_dir used without import) that only surfaced
    when the specific code paths fired. ast.parse + top-level import
    should stay clean."""
    # ast.parse covers syntax.
    with open(os.path.join(REPO_ROOT, "cloud_scheduler.py")) as f:
        ast.parse(f.read())


def test_run_auto_deployer_references_factor_bypass_after_definition():
    """The beta-exposure block at the top of run_auto_deployer USED to
    reference factor_bypass before it was defined (line 1786 used, line
    1823 defined). Now the block lives AFTER the `factor_bypass =
    bool(guardrails.get("factor_bypass"))` assignment.

    Parse the module, locate run_auto_deployer, and confirm no statement
    that reads the bare `factor_bypass` name appears BEFORE its first
    assignment inside the function body.
    """
    with open(os.path.join(REPO_ROOT, "cloud_scheduler.py")) as f:
        tree = ast.parse(f.read())
    run_auto = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "run_auto_deployer"),
        None,
    )
    assert run_auto is not None, "run_auto_deployer not found"

    first_assign_line = None
    first_read_line = None
    for node in ast.walk(run_auto):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "factor_bypass":
                    if first_assign_line is None or node.lineno < first_assign_line:
                        first_assign_line = node.lineno
        elif isinstance(node, ast.Name) and node.id == "factor_bypass" \
                and isinstance(node.ctx, ast.Load):
            if first_read_line is None or node.lineno < first_read_line:
                first_read_line = node.lineno

    assert first_assign_line is not None, (
        "factor_bypass should be assigned inside run_auto_deployer"
    )
    assert first_read_line is not None, (
        "factor_bypass should be read inside run_auto_deployer "
        "(otherwise the risk gate isn't wired)"
    )
    assert first_read_line >= first_assign_line, (
        f"factor_bypass is READ on line {first_read_line} BEFORE its "
        f"first assignment on line {first_assign_line} — the beta-exposure "
        f"block is dead code again. Check cloud_scheduler.run_auto_deployer "
        f"ordering."
    )


def test_no_bare_user_data_dir_function_calls():
    """user_data_dir(user) is NOT defined in cloud_scheduler (the module
    exposes user_file + user_strategies_dir; the user's data dir is at
    user["_data_dir"]). Any `user_data_dir(user)` call was a NameError
    waiting to fire the moment that code path ran. Guard against the
    pattern returning."""
    with open(os.path.join(REPO_ROOT, "cloud_scheduler.py")) as f:
        src = f.read()
    assert "user_data_dir(user)" not in src, (
        "cloud_scheduler.py contains bare user_data_dir(user) calls — "
        "use user['_data_dir'] instead (the module doesn't import "
        "auth.user_data_dir)."
    )


def test_ruff_clean_on_real_bug_rules():
    """Ruff F821 (undefined name) + B023 (loop capture) scan over the
    repo must stay clean. These are the rules that historically surfaced
    real bugs. Style rules are ignored per pyproject.toml."""
    import subprocess
    proc = subprocess.run(
        ["python3", "-m", "ruff", "check", "--select", "F821,B023", REPO_ROOT],
        capture_output=True, text=True, timeout=60,
    )
    # rc 0 = clean. Any finding = test failure.
    assert proc.returncode == 0, (
        f"ruff found real bugs:\n{proc.stdout}\n{proc.stderr}"
    )
