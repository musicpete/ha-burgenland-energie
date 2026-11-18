"""Fake Burgenland Energie API reproducing the formats observed on 18 Nov 2026."""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta

from aiohttp import web

CUSTOMER_NR = "000001"
CONTRACT = "00000000001"
SECOND_CONTRACT = "00000000002"
POD_A = "AT0000000000000000000000000000001"
POD_B = "AT0000000000000000000000000000002"
PDF = b"%PDF-1.4 fake invoice"


def make_jwt(customer_nr: str, exp: int, n: int) -> str:
    def enc(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return ".".join(
        [enc({"alg": "none"}), enc({"custom:customerNr": customer_nr, "email": "user@example.com", "exp": exp, "n": n}), "test-signature"]
    )


def refresh_token(n: int) -> str:
    # Synthetic placeholder; real refresh tokens never appear in this repository.
    return f"test-refresh-token-{n}-" + "x" * 1800


class FakeApi:
    def __init__(self) -> None:
        self.app = web.Application()
        self.base = ""
        self.rotate = False
        self.valid_refresh = {refresh_token(0)}
        self.issued = 0
        self.valid_id: set[str] = set()
        self.exp_offset = 3600
        self.fail_next: list[int] = []  # statuses returned by the next data requests
        self.calls: list[str] = []
        self.refresh_calls = 0
        r = self.app.router
        r.add_post("/papi/auth/signin/refresh", self.refresh)
        r.add_get("/api/contracts", self.contracts)
        r.add_get("/api/contracts/{nr}/bills", self.bills)
        r.add_get("/api/contracts/{nr}/bills/{bill}", self.bill_pdf)
        r.add_get("/api/contracts/{nr}/part-payment-plan", self.plan)
        r.add_get("/api/profile/bonus-points", self.bonus)
        r.add_get("/api/contracts/{nr}/meter-data/{pod}/v2", self.meter)

    # -- auth
    async def refresh(self, request: web.Request) -> web.Response:
        import time

        self.refresh_calls += 1
        body = await request.json()
        if body.get("refreshToken") not in self.valid_refresh:
            return web.json_response({"message": "Refresh Token has expired"}, status=400)
        self.issued += 1
        idt = make_jwt(CUSTOMER_NR, int(time.time()) + self.exp_offset, self.issued)
        self.valid_id.add(idt)
        out = {"idToken": idt, "accessToken": "acc", "expireIn": "3600"}
        if self.rotate:
            new = refresh_token(self.issued)
            self.valid_refresh = {new}
            out["refreshToken"] = new
        else:
            out["refreshToken"] = body["refreshToken"]
        return web.json_response(out)

    def _check(self, request: web.Request) -> web.Response | None:
        self.calls.append(request.path)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return web.json_response({"message": "internal"}, status=500)
        if auth not in self.valid_id:
            return web.json_response({"message": "Unauthorized"}, status=401)
        if self.fail_next:
            return web.json_response({"message": "fail"}, status=self.fail_next.pop(0))
        return None

    # -- data
    async def contracts(self, request: web.Request) -> web.Response:
        if (bad := self._check(request)) is not None:
            return bad
        assert request.query["customerNr"] == CUSTOMER_NR
        tariff = lambda name, typ, pod, wp, bp: {  # noqa: E731
            "basePrice": bp, "basePriceName": "P", "basePriceOperand": "TTPAUSCH_E", "boundUntil": "",
            "division": "10", "extUi": pod, "fcbeMember": True, "priceGuaranteeUntil": "",
            "tariffName": name, "tariffType": typ, "workPrice": wp, "workPrice2": 0,
        }
        return web.json_response(
            [
                {
                    "nr": CONTRACT, "description": "Musterstraße 1, 0000 Musterort", "electricity": True, "isActive": True,
                    "eBillingEmail": "u***@example.com",
                    "tariffs": [
                        tariff("Tarif A", "T-A", POD_A, 0.15, 3.0),
                        tariff("Tarif B Ä", "T-B", POD_B, 0.125, 0),
                    ],
                },
                {"nr": SECOND_CONTRACT, "description": "Vertrag 2", "electricity": False, "isActive": True, "tariffs": []},
            ]
        )

    async def bills(self, request: web.Request) -> web.Response:
        if (bad := self._check(request)) is not None:
            return bad
        if request.match_info["nr"] != CONTRACT:
            return web.json_response({"message": "no bills"}, status=404)
        return web.json_response(
            [
                {"billNr": "000000000003", "dateFrom": "2025-01-01", "dateTo": "2025-12-31", "amount": 1200.0, "amountCurr": "EUR", "electricity": False},
                {"billNr": "000000000002", "dateFrom": "2025-03-15", "dateTo": "9999-12-31", "amount": 40.0, "amountCurr": "EUR"},
                {"billNr": "000000000001", "dateFrom": "2024-03-10", "dateTo": "9999-12-31", "amount": 300.0, "amountCurr": "EUR"},
            ]
        )

    async def bill_pdf(self, request: web.Request) -> web.Response:
        if (bad := self._check(request)) is not None:
            return bad
        assert request.query.get("compress") == "false"
        return web.json_response({"data": base64.b64encode(PDF).decode()})

    async def plan(self, request: web.Request) -> web.Response:
        if (bad := self._check(request)) is not None:
            return bad
        items = []
        for i, due in enumerate(["2026-09-01", "2026-10-01", "2026-11-01", "2026-12-01", "2027-01-01"]):
            items.append({"amount": 100.0, "amountOpen": 100.0 if i < 3 else 0, "balancingStatus": "partiallyPaid" if i < 3 else "open", "division": 12, "dueDate": due, "settlementDate": ""})
        return web.json_response({"advancePayment": items})

    async def bonus(self, request: web.Request) -> web.Response:
        if (bad := self._check(request)) is not None:
            return bad
        return web.json_response({"bonuspoints": 1500, "factor": 0.1})

    async def meter(self, request: web.Request) -> web.Response:
        if (bad := self._check(request)) is not None:
            return bad
        start = date.fromisoformat(request.query["fromDate"])
        end = date.fromisoformat(request.query["toDate"])
        assert (end - start).days < 366
        per_day = 10.0 if request.match_info["pod"] == POD_A else 5.0
        out, d = [], start
        while d <= end:
            out.append({"description": d.isoformat(), "value": per_day})
            d += timedelta(days=1)
        return web.json_response(out)
