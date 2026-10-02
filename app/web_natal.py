from __future__ import annotations

from quart import Blueprint, abort, g, make_response

from app.natal.report_builder import build_hosted_report_html
from app.natal.report_ids import is_valid_report_id
from app.natal.storage import get_report

natal_bp = Blueprint("natal_reports", __name__)


@natal_bp.get("/reports/natal/<report_id>")
async def natal_report(report_id: str):
    if not is_valid_report_id(report_id):
        abort(404)
    report = await get_report(report_id)
    if report is None:
        abort(404)
    script_nonce = g.csp_nonce
    response = await make_response(build_hosted_report_html(report, script_nonce=script_nonce))
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'nonce-{script_nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none';"
    )
    return response
