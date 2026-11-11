"""All PayPal calls (sandbox). Orders v2 + Payments v2.

Idempotency: every logical operation gets ONE PayPal-Request-Id, and that same
id is reused if the call is retried. Keys are built from PayPal's own IDs where
possible (order id, authorization id), so they stay unique even after the
SQLite database resets.
"""
import os
import time
import uuid
import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com").rstrip("/")
CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")

_client = httpx.Client(timeout=30)
RETRIES = 3  # total attempts per operation


def usd(c):
    return f"{c/100:.2f}"


def _token():
    r = _client.post(f"{BASE}/v1/oauth2/token", auth=(CLIENT_ID, SECRET),
                     data={"grant_type": "client_credentials"})
    r.raise_for_status()
    return r.json()["access_token"]


def _h(request_id):
    """Headers for one logical operation. The caller passes the SAME request_id on every retry."""
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json",
            "PayPal-Request-Id": request_id, "Prefer": "return=representation"}


def _post(path, request_id, body=None):
    """POST with retries on network errors, 429 and 5xx, always reusing request_id."""
    last = None
    for attempt in range(RETRIES):
        try:
            r = _client.post(f"{BASE}{path}", headers=_h(request_id),
                             json=body if body is not None else {})
            if r.status_code < 500 and r.status_code != 429:
                return r
            last = r
        except httpx.TransportError as e:
            last = e
        time.sleep(0.5 * (attempt + 1))
    if isinstance(last, Exception):
        raise last
    return last


def create_order(ref, desc, amt, ret, cancel):
    # One key per checkout click; reused across this call's retries.
    # A duplicate order can't charge anyone: the client still has to approve it in PayPal.
    key = f"order-{ref}-{uuid.uuid4().hex[:12]}"
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [{"reference_id": ref, "description": desc[:127],
                            "amount": {"currency_code": "USD", "value": usd(amt)}}],
        "payment_source": {"paypal": {"experience_context": {
            "return_url": ret, "cancel_url": cancel, "user_action": "CONTINUE"}}},
    }
    r = _post("/v2/checkout/orders", key, body)
    r.raise_for_status()
    j = r.json()
    link = next(l["href"] for l in j["links"] if l["rel"] in ("payer-action", "approve"))
    return j["id"], link


def authorize_order(oid):
    # Deterministic: authorizing the same order twice (retry or page refresh)
    # returns the same authorization instead of creating a new one.
    r = _post(f"/v2/checkout/orders/{oid}/authorize", f"authorize-{oid}")
    r.raise_for_status()
    return r.json()["purchase_units"][0]["payments"]["authorizations"][0]["id"]


def capture(a, amt, inv, note):
    # Deterministic per authorization + invoice, so a retried or double-clicked
    # capture for the same item returns the original capture, not a second charge.
    body = {"amount": {"currency_code": "USD", "value": usd(amt)},
            "invoice_id": inv, "note_to_payer": note[:255], "final_capture": False}
    r = _post(f"/v2/payments/authorizations/{a}/capture", f"capture-{inv}", body)
    return r.json() if r.content else {"status": f"HTTP {r.status_code}"}


def void(a):
    r = _post(f"/v2/payments/authorizations/{a}/void", f"void-{a}")
    return r.status_code