"""Invoice PDF archive: file names, verification, serving and signed links.

PDFs live in ``<media>/burgenland_energie/`` (owner-only file mode). They
are served by an authenticated view; dashboards get per-invoice links that
are signed by Home Assistant, valid for one file only and for a limited time,
so they open in a browser tab without exposing anything else.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback

from .api import Bill, valid_id
from .const import DOMAIN, INVOICE_DIR

INVOICE_PATH = f"/api/{DOMAIN}/invoice"
LINK_VALIDITY = timedelta(hours=24)
_VIEW_KEY = f"{DOMAIN}_invoice_view"


def invoice_dir(hass: HomeAssistant) -> Path:
    """<media>/burgenland_energie - the "local" media source if configured."""
    media = hass.config.media_dirs.get("local") or next(iter(hass.config.media_dirs.values()), None)
    return Path(media or hass.config.path("media")) / INVOICE_DIR


def invoice_filename(bill: Bill) -> str:
    # bill_nr and the date are validated in api.parse_bills.
    kind = "Jahresabrechnung" if bill.is_annual else "Rechnung"
    return f"{bill.date_from.isoformat()}_{kind}_{bill.bill_nr}.pdf"


def verified_files(folder: Path, bills: list[Bill]) -> dict[str, str]:
    """bill_nr -> file name for every invoice saved as a real PDF (blocking)."""
    found: dict[str, str] = {}
    for bill in bills:
        path = folder / invoice_filename(bill)
        try:
            with path.open("rb") as fh:
                if fh.read(5) == b"%PDF-" and path.stat().st_size > 1024:
                    found[bill.bill_nr] = path.name
        except OSError:
            continue
    return found


def write_pdf(folder: Path, name: str, pdf: bytes) -> Path:
    """Write atomically with owner-only permissions (blocking)."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / name
    tmp = path.with_suffix(".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pdf)
    os.replace(tmp, path)
    return path


@callback
def async_register_view(hass: HomeAssistant) -> None:
    if not hass.data.get(_VIEW_KEY):
        hass.http.register_view(InvoiceView())
        hass.data[_VIEW_KEY] = True


@callback
def signed_links(hass: HomeAssistant, bill_nrs: list[str]) -> dict[str, str]:
    """Per-invoice links that open the PDF without an auth header."""
    return {
        nr: async_sign_path(hass, f"{INVOICE_PATH}/{nr}", LINK_VALIDITY, use_content_user=True)
        for nr in bill_nrs
    }


class InvoiceView(HomeAssistantView):
    """Serves one saved invoice PDF. Requires a logged-in user or a signed link."""

    url = INVOICE_PATH + "/{bill_nr}"
    name = f"api:{DOMAIN}:invoice"
    requires_auth = True

    async def get(self, request: web.Request, bill_nr: str) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        nr = valid_id(bill_nr)
        if nr is None:
            return web.Response(status=404)
        folder = invoice_dir(hass)

        def _find() -> Path | None:
            if not folder.is_dir():
                return None
            for path in folder.glob(f"*_{nr}.pdf"):
                if path.is_file() and path.parent == folder:
                    return path
            return None

        path = await hass.async_add_executor_job(_find)
        if path is None:
            return web.Response(status=404)
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": f'inline; filename="{path.name}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
