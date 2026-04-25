"""Round-61 pt.76 — signup form: invite-code section cleanup.

User-reported: the Invite Code area showed the label twice
("INVITE CODE" as section header AND as input <label>) and the
help text was too verbose. Pt.76 dedupes the label and shortens
the help text to a single sentence.
"""
from __future__ import annotations

import pathlib


_HERE = pathlib.Path(__file__).resolve().parent.parent
_SIGNUP = (_HERE / "templates" / "signup.html").read_text()


def test_invite_code_label_visually_hidden_not_duplicated():
    """The section header is the VISIBLE "Invite code" label. The
    a11y `<label for="invite_code">` is preserved (pt.8 contract)
    but its inner text is wrapped in `.pt76-sr-only` so it doesn't
    duplicate the section header on screen. Pt.8's source-pin in
    test_round61_pt8_audit_concurrency_fixes still passes."""
    # Section header still visible.
    assert '<div class="section-title">Invite code</div>' in _SIGNUP
    # Label still present (pt.8 a11y contract).
    assert '<label for="invite_code">' in _SIGNUP
    # Label text wrapped in sr-only span so it's invisible.
    assert '<span class="pt76-sr-only">Invite code</span></label>' in _SIGNUP
    # The hidden-label class itself is defined.
    assert ".pt76-sr-only" in _SIGNUP


def test_pt76_sr_only_class_uses_standard_pattern():
    """The sr-only utility uses the standard CSS pattern (1px
    clipped, position absolute) so screen readers still pick it up."""
    idx = _SIGNUP.find(".pt76-sr-only")
    assert idx > 0
    block = _SIGNUP[idx:idx + 400]
    assert "position:absolute" in block
    assert "clip:" in block
    assert "width:1px" in block


def test_invite_code_help_text_is_concise():
    """The help text should be a single short sentence, not the
    long pre-pt.76 wall about admin links + SIGNUP_INVITE_CODE."""
    # Find the invite-code form-group block.
    idx = _SIGNUP.find('id="invite_code"')
    assert idx > 0
    # Look at the surrounding ~600 chars for the help text.
    block_start = max(0, idx - 100)
    block_end = min(len(_SIGNUP), idx + 600)
    block = _SIGNUP[block_start:block_end]
    # New short help text present.
    assert "Only required if an admin sent you" in block
    # Old verbose help text removed.
    assert "SIGNUP_INVITE_CODE" not in block
    assert "auto-filled from the URL" not in block


def test_invite_code_input_kept_intact():
    """Pt.76 only changed the label / help-text. The input itself
    (id, name, autocomplete, placeholder existence) stays so the
    auto-fill-from-URL JS at the bottom of the file still works."""
    assert 'id="invite_code"' in _SIGNUP
    # Auto-fill from ?invite= URL param is still wired.
    assert "params.get('invite')" in _SIGNUP


def test_invite_code_section_title_uses_sentence_case():
    """Pt.74 standardized on sentence-case section titles ("Account",
    "Email Notifications"). The invite-code title now matches."""
    assert '<div class="section-title">Invite code</div>' in _SIGNUP
    # The OLD title-case "Invite Code" header is gone.
    assert '<div class="section-title">Invite Code</div>' not in _SIGNUP
