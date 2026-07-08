from app.natal.report_ids import is_valid_report_id


def test_valid_report_id_accepts_urlsafe_token_like_values():
    assert is_valid_report_id("abcDEF0123456789_-")


def test_valid_report_id_rejects_route_incompatible_values():
    assert not is_valid_report_id("short")
    assert not is_valid_report_id("invalid.report.id")
    assert not is_valid_report_id("has/slash")
    assert not is_valid_report_id("x" * 129)
