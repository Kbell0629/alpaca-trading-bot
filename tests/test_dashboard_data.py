"""get_dashboard_data + its decomposed helpers."""
import os
import json


def test_resolve_user_paths_defaults_to_data_dir_for_env_mode(isolated_data_dir):
    import server
    user_dir, strats_dir = server._resolve_user_paths(None)
    assert user_dir == server.DATA_DIR
    assert strats_dir == server.STRATEGIES_DIR


def test_resolve_user_paths_creates_per_user_dir(isolated_data_dir):
    import auth, server
    uid, _ = auth.create_user(
        email="d@example.com", username="duser",
        password="correct horse battery staple!!",
        alpaca_key="k", alpaca_secret="s",
    )
    user_dir, strats_dir = server._resolve_user_paths(uid)
    assert os.path.isdir(user_dir)
    assert os.path.isdir(strats_dir)
    assert str(uid) in user_dir  # scoped to user


def test_load_with_shared_fallback_non_admin_never_reads_shared(isolated_data_dir):
    """Regression test for the round-3 cross-user migration leak.
    A non-admin user must NEVER read the shared STRATEGIES_DIR file."""
    import server

    # Set up a shared file that user_id=2 should NOT see
    shared = os.path.join(isolated_data_dir, "leak_probe.json")
    with open(shared, "w") as f:
        json.dump({"owner": "shared_admin"}, f)

    per_user = os.path.join(isolated_data_dir, "users/2/leak_probe.json")
    os.makedirs(os.path.dirname(per_user), exist_ok=True)

    result = server._load_with_shared_fallback(per_user, shared, user_id=2)
    assert result is None, \
        "non-admin user read the shared file — SECURITY REGRESSION"


def test_trading_session_is_computed_live_not_from_stale_json(isolated_data_dir):
    """Regression: the dashboard badge was stuck on "MARKET OPEN" for
    46+ minutes after the 4 PM close because trading_session was only
    refreshed when the screener ran (every 30 min during market hours).
    After close, no more screener runs fire, so the stored value
    stayed at "market" all evening. Fixed by overwriting on every
    get_dashboard_data() call.
    """
    import os, json, server
    uid = 1
    user_dir, _ = server._resolve_user_paths(uid)

    # Seed a dashboard_data.json with a stale "market" value plus
    # enough scaffolding that get_dashboard_data() picks it up.
    stale_data = {
        "picks": [],
        "total_screened": 0,
        "total_passed": 0,
        "trading_session": "market",  # stale value — bot thinks market is open
    }
    with open(os.path.join(user_dir, "dashboard_data.json"), "w") as f:
        json.dump(stale_data, f)

    # get_dashboard_data should OVERWRITE the stale value with whatever
    # extended_hours.get_trading_session() returns right now.
    result = server.get_dashboard_data(user_id=uid)
    from extended_hours import get_trading_session
    expected = get_trading_session()
    assert result["trading_session"] == expected, (
        f"dashboard stuck on stale session: got {result['trading_session']!r} "
        f"instead of live {expected!r}"
    )


def test_load_with_shared_fallback_admin_copies_on_first_read(isolated_data_dir):
    """user_id=1 (bootstrap admin) may migrate shared → per-user once."""
    import server

    shared = os.path.join(isolated_data_dir, "cfg.json")
    with open(shared, "w") as f:
        json.dump({"legacy": True}, f)

    per_user = os.path.join(isolated_data_dir, "users/1/cfg.json")
    os.makedirs(os.path.dirname(per_user), exist_ok=True)

    result = server._load_with_shared_fallback(per_user, shared, user_id=1)
    assert result == {"legacy": True}
    # Admin migration also COPIES the file so subsequent reads are per-user
    assert os.path.exists(per_user)
