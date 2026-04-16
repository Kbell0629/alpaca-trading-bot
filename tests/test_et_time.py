"""ET-only policy enforcement."""


def test_now_et_is_tz_aware_and_eastern(isolated_data_dir):
    import et_time
    now = et_time.now_et()
    assert now.tzinfo is not None
    # tzname is EDT or EST depending on the season
    tz = now.tzname() or ""
    assert tz in ("EDT", "EST"), f"expected ET, got {tz!r}"


def test_iso_format_includes_offset(isolated_data_dir):
    import et_time
    s = et_time.now_et().isoformat()
    # -04:00 in summer, -05:00 in winter — never +00:00
    assert s.endswith("-04:00") or s.endswith("-05:00"), f"bad offset: {s}"


def test_extended_hours_uses_correct_tz(isolated_data_dir):
    # This is a REGRESSION test for the round-5 DST bug:
    # extended_hours.get_trading_session() previously hardcoded UTC-4 which
    # broke half the year during EST. We don't test return value (depends
    # on clock), we just verify it calls into zoneinfo-backed helper.
    import extended_hours, et_time
    # If get_trading_session ever goes back to manual offset arithmetic,
    # this will fail because extended_hours should use _now_et()
    src = open(extended_hours.__file__).read()
    assert "timedelta(hours=-4)" not in src, \
        "extended_hours must NOT use hardcoded -4h offset (DST bug)"
    assert "_now_et" in src or "now_et" in src
