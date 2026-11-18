"""One-click token handover from the portal to a running config flow.

The portal login is captcha-protected, so the user signs in there
themselves. A bookmarklet then opens a Home Assistant page and passes the
portal's refresh token in the URL *fragment* (never sent to any server, so
it cannot appear in access logs). That page posts it once to this view,
which feeds it into the waiting config flow.

Safeguards:
- one random 256-bit secret per flow, single use, valid for 15 minutes;
- the token arrives only in a POST body, size- and format-checked;
- the bookmarklet only runs on the portal's own origin;
- the page sends no referrer, is never cached and allows only its own script.
"""

from __future__ import annotations

import html
import json
import secrets
import time
from dataclasses import dataclass

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import UnknownFlow

from .api import PORTAL_ORIGIN
from .const import CONF_REFRESH_TOKEN, DOMAIN

HANDOVER_PATH = f"/api/{DOMAIN}/handover"
TTL_S = 15 * 60
MAX_BODY = 16 * 1024
TOKEN_MIN, TOKEN_MAX = 100, 8000
_DATA_KEY = f"{DOMAIN}_handover"
_TOKEN_KEY = "okc-auth-refreshToken"


@dataclass
class _Pending:
    flow_id: str
    expires: float


def _pending(hass: HomeAssistant) -> dict[str, _Pending]:
    data = hass.data.setdefault(_DATA_KEY, {"view": False, "pending": {}})
    if not data["view"]:
        hass.http.register_view(TokenHandoverView())
        data["view"] = True
    return data["pending"]


@callback
def async_create(hass: HomeAssistant, flow_id: str) -> str:
    """Register a one-time handover for this flow; returns the page path."""
    pending = _pending(hass)
    now = time.monotonic()
    for key in [k for k, p in pending.items() if p.expires < now or p.flow_id == flow_id]:
        pending.pop(key)
    secret = secrets.token_urlsafe(32)
    pending[secret] = _Pending(flow_id, now + TTL_S)
    return f"{HANDOVER_PATH}/{secret}"


@callback
def async_discard(hass: HomeAssistant, flow_id: str) -> None:
    """Drop the handover of a finished or aborted flow."""
    pending = hass.data.get(_DATA_KEY, {}).get("pending", {})
    for key in [k for k, p in pending.items() if p.flow_id == flow_id]:
        pending.pop(key)


def valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and TOKEN_MIN <= len(value) <= TOKEN_MAX
        and value.isascii()
        and not any(c.isspace() for c in value)
    )


_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _page(nonce: str, body: str, script: str = "") -> web.Response:
    csp = (
        "default-src 'none'; style-src 'unsafe-inline'; "
        f"script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'"
    )
    text = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Burgenland Energie – Token-Übergabe</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem;color:#1c1c1c}}
a.bm{{display:inline-block;padding:.5rem 1rem;border-radius:8px;background:#03a9f4;color:#fff;text-decoration:none;font-weight:600}}
.ok{{color:#2e7d32}} .err{{color:#c62828}} ol li{{margin:.4rem 0}} small{{color:#666}}
</style></head><body>{body}{script}</body></html>"""
    return web.Response(text=text, content_type="text/html", headers={**_HEADERS, "Content-Security-Policy": csp})


class TokenHandoverView(HomeAssistantView):
    """Serves the handover page and receives the token (no HA login: the
    bookmarklet runs in the portal tab; the one-time secret authorizes)."""

    url = HANDOVER_PATH + "/{secret}"
    name = f"api:{DOMAIN}:handover"
    requires_auth = False

    def _lookup(self, request: web.Request, secret: str) -> _Pending | None:
        pending = request.app[KEY_HASS].data.get(_DATA_KEY, {}).get("pending", {})
        entry = pending.get(secret)
        if entry is None or entry.expires < time.monotonic():
            return None
        return entry

    async def get(self, request: web.Request, secret: str) -> web.Response:
        nonce = secrets.token_urlsafe(16)
        if self._lookup(request, secret) is None:
            return _page(
                nonce,
                "<h1>Link abgelaufen</h1><p class='err'>Dieser Übergabe-Link ist nicht mehr "
                "gültig. Starte die Einrichtung in Home Assistant neu.</p>",
            )
        portal = json.dumps(PORTAL_ORIGIN)
        token_key = json.dumps(_TOKEN_KEY)
        body = f"""<h1>Burgenland Energie – Token-Übergabe</h1>
<div id="steps">
<ol>
<li>Ziehe diesen Button in die Lesezeichenleiste: <a class="bm" id="bm" href="#">Token an Home Assistant</a></li>
<li>Öffne <a href="{html.escape(PORTAL_ORIGIN)}" target="_blank" rel="noopener noreferrer">meine.burgenlandenergie.at</a>,
melde dich an und setze <b>Angemeldet bleiben</b>.</li>
<li>Klicke dort auf das Lesezeichen. Es öffnet sich ein Tab, der die Einrichtung abschließt.</li>
</ol>
<p><small>Der Link gilt einmalig und 15 Minuten. Der Token wird nur an diese
Home-Assistant-Instanz übergeben.</small></p>
</div>
<p id="status"></p>"""
        script = f"""<script nonce="{nonce}">
(function () {{
  var page = location.origin + location.pathname;
  var status = document.getElementById("status");
  var hash = location.hash.slice(1);
  if (hash) {{
    history.replaceState(null, "", location.pathname);   // drop the token from the address bar
    document.getElementById("steps").hidden = true;
    status.textContent = "Übergabe läuft …";
    fetch(page, {{method: "POST", headers: {{"Content-Type": "application/json"}},
                 body: JSON.stringify({{token: decodeURIComponent(hash)}}), credentials: "omit"}})
      .then(function (r) {{
        status.className = r.ok ? "ok" : "err";
        status.textContent = r.ok
          ? "Fertig. Du kannst diesen Tab schließen und in Home Assistant weitermachen."
          : "Die Übergabe wurde abgelehnt (" + r.status + "). Starte die Einrichtung neu.";
      }})
      .catch(function () {{ status.className = "err"; status.textContent = "Home Assistant nicht erreichbar."; }});
    return;
  }}
  var bm = "javascript:(function(){{if(location.origin!==" + JSON.stringify({portal}) +
    "){{alert('Bitte auf meine.burgenlandenergie.at verwenden.');return}}" +
    "var t=localStorage.getItem(" + JSON.stringify({token_key}) + ");" +
    "if(!t){{alert('Bitte zuerst im Portal anmelden.');return}}" +
    "window.open(" + JSON.stringify(page) + "+'#'+encodeURIComponent(t),'_blank','noopener')}})()";
  document.getElementById("bm").setAttribute("href", bm);
}})();
</script>"""
        return _page(nonce, body, script)

    async def post(self, request: web.Request, secret: str) -> web.Response:
        hass = request.app[KEY_HASS]
        entry = self._lookup(request, secret)
        if entry is None:
            return web.json_response({"error": "expired"}, status=404, headers=_HEADERS)
        if (request.content_length or 0) > MAX_BODY:
            return web.json_response({"error": "too_large"}, status=413, headers=_HEADERS)
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "bad_request"}, status=400, headers=_HEADERS)
        token = body.get("token") if isinstance(body, dict) else None
        if not valid_token(token):
            return web.json_response({"error": "invalid_token"}, status=400, headers=_HEADERS)
        # Single use: consumed before the flow continues.
        hass.data[_DATA_KEY]["pending"].pop(secret, None)
        try:
            await hass.config_entries.flow.async_configure(
                flow_id=entry.flow_id, user_input={CONF_REFRESH_TOKEN: token}
            )
        except UnknownFlow:
            return web.json_response({"error": "flow_gone"}, status=410, headers=_HEADERS)
        return web.json_response({"ok": True}, headers=_HEADERS)
