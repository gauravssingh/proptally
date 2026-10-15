from proptally.web.helpers import format_bps


def test_format_bps_uses_legacy_milli_percent_scale():
    assert format_bps(3125) == "3.125%"
    assert format_bps(2500) == "2.500%"
    assert format_bps(6250) == "6.250%"
    assert format_bps(None) == "—"
