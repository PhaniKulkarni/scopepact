from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from app.models import SessionLocal, Pact, LineItem, Payment, Event
from app import paypal

app = FastAPI(title="ScopePact")
API = "http://localhost:8000"

class ItemIn(BaseModel):
    name: str
    price_cents: int

class PactIn(BaseModel):
    title: str
    items: list[ItemIn]

def log(db, pact_id, type_, detail):
    db.add(Event(pact_id=pact_id, type=type_, detail=detail))

@app.post("/pacts")
def create_pact(p: PactIn):
    with SessionLocal() as db:
        pact = Pact(title=p.title)
        db.add(pact); db.flush()
        for i in p.items:
            db.add(LineItem(pact_id=pact.id, name=i.name, price_cents=i.price_cents))
        log(db, pact.id, "pact_created", f"{p.title} with {len(p.items)} items")
        db.commit()
        return {"pact_id": pact.id}

@app.post("/pacts/{pid}/checkout")
def checkout(pid: int):
    with SessionLocal() as db:
        pact = db.get(Pact, pid)
        unheld = [i for i in pact.items if i.auth_id is None and i.status == "agreed"]
        total = sum(i.price_cents for i in unheld)
        if total == 0:
            raise HTTPException(400, "Nothing new to authorize")
        order_id, link = paypal.create_order(
            f"pact-{pid}-v{pact.version}", pact.title, total,
            f"{API}/paypal/return?pact_id={pid}", f"{API}/paypal/cancel")
        db.add(Payment(pact_id=pid, kind="order", paypal_id=order_id, amount_cents=total))
        log(db, pid, "order_created", f"{order_id} for {paypal.usd(total)}")
        db.commit()
        return {"approve_link": link, "amount": paypal.usd(total)}

@app.get("/paypal/return")
def paypal_return(token: str, pact_id: int):
    with SessionLocal() as db:
        auth_id = paypal.authorize_order(token)
        pact = db.get(Pact, pact_id)
        held = 0
        for i in pact.items:
            if i.auth_id is None and i.status == "agreed":
                i.auth_id = auth_id
                held += i.price_cents
        pact.status = "active"
        db.add(Payment(pact_id=pact_id, kind="authorization", paypal_id=auth_id,
                       amount_cents=held, status="CREATED"))
        log(db, pact_id, "funds_held", f"{auth_id} holding {paypal.usd(held)}")
        db.commit()
        return {"message": "Funds held. You can close this tab.", "authorization_id": auth_id}

@app.post("/items/{iid}/verify")   # temporary: the AI proof checker replaces this later
def verify(iid: int):
    with SessionLocal() as db:
        item = db.get(LineItem, iid)
        item.status = "verified"
        log(db, item.pact_id, "item_verified", item.name)
        db.commit()
        return {"item": item.name, "status": item.status}

@app.post("/items/{iid}/capture")
def capture_item(iid: int):
    with SessionLocal() as db:
        item = db.get(LineItem, iid)
        # Policy rule: only verified, held items can be paid
        if item.status != "verified":
            raise HTTPException(400, f"Blocked: item is '{item.status}', not verified")
        if not item.auth_id:
            raise HTTPException(400, "Blocked: no PayPal hold for this item")
        r = paypal.capture(item.auth_id, item.price_cents,
                           f"pact-{item.pact_id}-item-{item.id}", f"{item.name} verified")
        if r.get("status") != "COMPLETED":
            raise HTTPException(502, f"PayPal capture failed: {r}")
        item.status = "paid"
        db.add(Payment(pact_id=item.pact_id, item_id=item.id, kind="capture",
                       paypal_id=r["id"], amount_cents=item.price_cents, status="COMPLETED"))
        log(db, item.pact_id, "item_paid", f"{item.name} {paypal.usd(item.price_cents)}")
        db.commit()
        return {"item": item.name, "captured": paypal.usd(item.price_cents), "capture_id": r["id"]}

@app.post("/pacts/{pid}/close")
def close(pid: int):
    with SessionLocal() as db:
        pact = db.get(Pact, pid)
        for auth_id in {i.auth_id for i in pact.items if i.auth_id}:
            code = paypal.void(auth_id)
            db.add(Payment(pact_id=pid, kind="void", paypal_id=auth_id, status=str(code)))
        pact.status = "closed"
        log(db, pid, "pact_closed", "unused holds released")
        db.commit()
        return {"status": "closed"}

@app.get("/pacts/{pid}")
def get_pact(pid: int):
    with SessionLocal() as db:
        pact = db.get(Pact, pid)
        return {
            "title": pact.title, "status": pact.status, "version": pact.version,
            "items": [{"id": i.id, "name": i.name, "price": paypal.usd(i.price_cents),
                       "status": i.status} for i in pact.items],
            "payments": [{"kind": p.kind, "paypal_id": p.paypal_id,
                          "amount": paypal.usd(p.amount_cents)}
                         for p in db.query(Payment).filter_by(pact_id=pid)],
            "history": [f"{e.created_at:%H:%M:%S} {e.type}: {e.detail}"
                        for e in db.query(Event).filter_by(pact_id=pid)],
        }