"""Card payment processors behind one interface: `charge(amount, currency, token, description)` and
`refund(charge_id, amount)`, both returning {"id", "status", ...} or raising ProcessorError.

- SimulatedProcessor (default): approves everything except the test token "tok_decline" and amounts above
  SIM_DECLINE_ABOVE; ids are "sim_ch_..." / "sim_re_...". No network.
- StripeProcessor (PAYMENT_PROCESSOR=stripe and STRIPE_SECRET_KEY set): PaymentIntents API, confirmed server-side with a
  payment-method token from the client (Stripe.js / Terminal); refunds through /v1/refunds. Amounts in minor units.
Card data never touches this service: the client tokenises with the processor, we only see the token.
"""
from __future__ import annotations

import uuid

import httpx

from restaurant_ops.config import settings

SIM_DECLINE_ABOVE = 5000.0


class ProcessorError(RuntimeError):
    pass


class SimulatedProcessor:
    name = "simulated"

    def charge(self, amount: float, currency: str = "usd", token: str | None = None, description: str = "") -> dict:
        if token == "tok_decline" or amount > SIM_DECLINE_ABOVE:
            raise ProcessorError("card declined (simulated)")
        return {"id": f"sim_ch_{uuid.uuid4().hex[:12]}", "status": "succeeded", "amount": round(amount, 2)}

    def refund(self, charge_id: str, amount: float) -> dict:
        if not charge_id:
            raise ProcessorError("nothing to refund against")
        return {"id": f"sim_re_{uuid.uuid4().hex[:12]}", "status": "succeeded", "charge": charge_id, "amount": round(amount, 2)}


class StripeProcessor:
    name = "stripe"
    API = "https://api.stripe.com/v1"

    def __init__(self, secret_key: str, client: httpx.Client | None = None):
        if not secret_key:
            raise ProcessorError("STRIPE_SECRET_KEY is not set")
        self.client = client or httpx.Client(timeout=settings.PAYMENT_TIMEOUT_S)
        self.auth = (secret_key, "")

    def _post(self, path: str, data: dict, idem: str) -> dict:
        try:
            r = self.client.post(f"{self.API}{path}", data=data, auth=self.auth, headers={"Idempotency-Key": idem})
        except httpx.HTTPError as e:
            raise ProcessorError(f"processor unreachable: {type(e).__name__}") from e
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            raise ProcessorError((body.get("error") or {}).get("message") or f"processor error {r.status_code}")
        return body

    def charge(self, amount: float, currency: str = "usd", token: str | None = None, description: str = "") -> dict:
        body = self._post("/payment_intents", {
            "amount": int(round(amount * 100)), "currency": currency.lower(), "payment_method": token or "pm_card_visa",
            "confirm": "true", "description": description[:200],
            "automatic_payment_methods[enabled]": "true", "automatic_payment_methods[allow_redirects]": "never",
        }, idem=f"pi-{uuid.uuid4().hex}")
        if body.get("status") != "succeeded":
            raise ProcessorError(f"payment {body.get('status', 'failed')}")
        return {"id": body["id"], "status": body["status"], "amount": body.get("amount", 0) / 100}

    def refund(self, charge_id: str, amount: float) -> dict:
        body = self._post("/refunds", {"payment_intent": charge_id, "amount": int(round(amount * 100))}, idem=f"re-{uuid.uuid4().hex}")
        return {"id": body.get("id"), "status": body.get("status"), "charge": charge_id, "amount": amount}


def make_processor():
    if settings.PAYMENT_PROCESSOR == "stripe":
        return StripeProcessor(settings.STRIPE_SECRET_KEY)
    return SimulatedProcessor()
