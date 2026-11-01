import os, uuid, httpx
from dotenv import load_dotenv

load_dotenv()
BASE = os.getenv("PAYPAL_BASE")

def get_token():
    r = httpx.post(
        f"{BASE}/v1/oauth2/token",
        auth=(os.getenv("PAYPAL_CLIENT_ID"), os.getenv("PAYPAL_CLIENT_SECRET")),
        data={"grant_type": "client_credentials"},
    )
    r.raise_for_status()
    return r.json()["access_token"]

def headers(token):
    # PayPal-Request-Id prevents double charges if a request is retried
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": str(uuid.uuid4())}

token = get_token()
print("1. Token OK")

# --- STEP A: create an order that only HOLDS money ---
order = httpx.post(f"{BASE}/v2/checkout/orders", headers=headers(token), json={
    "intent": "AUTHORIZE",
    "purchase_units": [{
        "reference_id": "pact-1-v1",
        "description": "ScopePact: landing page project test",
        "amount": {"currency_code": "USD", "value": "3.00"},
    }],
    "payment_source": {"paypal": {"experience_context": {
        "return_url": "https://example.com/approved",
        "cancel_url": "https://example.com/cancelled",
        "user_action": "CONTINUE",
        "shipping_preference": "NO_SHIPPING",
    }}},
}).json()

order_id = order["id"]
approve = next(l["href"] for l in order["links"] if l["rel"] == "payer-action")
print("2. Order created:", order_id)
print("\n   Open this link, log in as the CLIENT, and approve:\n  ", approve)
input("\n   After you land on example.com, press Enter here...")

# --- STEP B: authorize (place the hold) ---
auth = httpx.post(f"{BASE}/v2/checkout/orders/{order_id}/authorize",
                  headers=headers(token), json={}).json()
if auth.get("status") != "COMPLETED":
    print("Authorize failed:", auth); raise SystemExit
auth_id = auth["purchase_units"][0]["payments"]["authorizations"][0]["id"]
print("3. Authorized $3 hold. Authorization ID:", auth_id)

# --- STEP C: capture ONLY one verified line item ($300) ---
cap = httpx.post(f"{BASE}/v2/payments/authorizations/{auth_id}/capture",
                 headers=headers(token), json={
    "amount": {"currency_code": "USD", "value": "1.00"},
    "final_capture": False,
    "invoice_id": f"pact-1-item-1-{uuid.uuid4().hex[:6]}",
    "note_to_payer": "Homepage design, verified against brief",
}).json()
print("4. Captured $1 for item 1. Capture ID:", cap.get("id"), "| status:", cap.get("status"))

# --- STEP D: release the unused hold ---
v = httpx.post(f"{BASE}/v2/payments/authorizations/{auth_id}/void",
               headers=headers(token))
print("5. Voided remaining hold. HTTP status:", v.status_code, "(204 = success)")