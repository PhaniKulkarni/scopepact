import os, uuid, httpx
from dotenv import load_dotenv

load_dotenv()
BASE = os.getenv("PAYPAL_BASE")

def _token():
    r = httpx.post(f"{BASE}/v1/oauth2/token",
        auth=(os.getenv("PAYPAL_CLIENT_ID"), os.getenv("PAYPAL_CLIENT_SECRET")),
        data={"grant_type": "client_credentials"})
    r.raise_for_status()
    return r.json()["access_token"]

def _h():
    return {"Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": str(uuid.uuid4())}

def usd(cents):
    return f"{cents / 100:.2f}"

def create_order(reference, description, amount_cents, return_url, cancel_url):
    r = httpx.post(f"{BASE}/v2/checkout/orders", headers=_h(), json={
        "intent": "AUTHORIZE",
        "purchase_units": [{"reference_id": reference, "description": description,
                            "amount": {"currency_code": "USD", "value": usd(amount_cents)}}],
        "payment_source": {"paypal": {"experience_context": {
            "return_url": return_url, "cancel_url": cancel_url,
            "user_action": "CONTINUE", "shipping_preference": "NO_SHIPPING"}}},
    }).json()
    link = next(l["href"] for l in r["links"] if l["rel"] == "payer-action")
    return r["id"], link

def authorize_order(order_id):
    r = httpx.post(f"{BASE}/v2/checkout/orders/{order_id}/authorize",
                   headers=_h(), json={}).json()
    if r.get("status") != "COMPLETED":
        raise RuntimeError(f"Authorize failed: {r}")
    return r["purchase_units"][0]["payments"]["authorizations"][0]["id"]

def capture(auth_id, amount_cents, invoice_id, note):
    return httpx.post(f"{BASE}/v2/payments/authorizations/{auth_id}/capture",
        headers=_h(), json={
            "amount": {"currency_code": "USD", "value": usd(amount_cents)},
            "final_capture": False, "invoice_id": invoice_id, "note_to_payer": note,
        }).json()

def void(auth_id):
    return httpx.post(f"{BASE}/v2/payments/authorizations/{auth_id}/void",
                      headers=_h()).status_code