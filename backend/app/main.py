import json
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from app.models import (SessionLocal, Pact, LineItem, Payment, Event,
                        Rules, ChangeRequest, NegotiationTurn)
from app import paypal, policy, referee, agents

app = FastAPI(title="ScopePact")
API = "http://localhost:8000"


# ---------------------------------------------------------------
# Input shapes
# ---------------------------------------------------------------
class ItemIn(BaseModel):
    name: str
    price_cents: int


class PactIn(BaseModel):
    title: str
    items: list[ItemIn]


class RulesIn(BaseModel):
    side: str        # buyer or seller
    rules: dict


class ChangeIn(BaseModel):
    item_name: str
    asked_cents: int
    reason: str = ""
    buyer_max_cents: int     # produced by the buyer AI agent (or typed for testing)
    seller_min_cents: int    # produced by the seller AI agent (or typed for testing)
    buyer_message: str = ""
    seller_message: str = ""


class AiChangeIn(BaseModel):
    message: str             # the seller's casual text


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def log(db, pact_id, type_, detail):
    db.add(Event(pact_id=pact_id, type=type_, detail=detail))


def get_rules(db, pid, side):
    r = db.query(Rules).filter_by(pact_id=pid, side=side).first()
    return json.loads(r.rules_json) if r else {}


def current_total(pact):
    return sum(i.price_cents for i in pact.items if i.status != "released")


def add_item_from_change(db, pact, cr):
    pact.version += 1
    db.add(LineItem(pact_id=pact.id, name=cr.item_name,
                    price_cents=cr.agreed_cents, version=pact.version))
    log(db, pact.id, "pact_version",
        f"v{pact.version}: added {cr.item_name} at {paypal.usd(cr.agreed_cents)}")


def get_pact_or_404(db, pid):
    pact = db.get(Pact, pid)
    if not pact:
        raise HTTPException(404, f"Pact {pid} not found")
    return pact


# ---------------------------------------------------------------
# Pacts and payments
# ---------------------------------------------------------------
@app.post("/pacts")
def create_pact(p: PactIn):
    with SessionLocal() as db:
        pact = Pact(title=p.title)
        db.add(pact)
        db.flush()
        for i in p.items:
            db.add(LineItem(pact_id=pact.id, name=i.name, price_cents=i.price_cents))
        log(db, pact.id, "pact_created", f"{p.title} with {len(p.items)} items")
        db.commit()
        return {"pact_id": pact.id}


@app.post("/pacts/{pid}/checkout")
def checkout(pid: int):
    with SessionLocal() as db:
        pact = get_pact_or_404(db, pid)
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
        pact = get_pact_or_404(db, pact_id)
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
        return {"message": "Funds held. You can close this tab.",
                "authorization_id": auth_id, "held": paypal.usd(held)}


@app.get("/paypal/cancel")
def paypal_cancel():
    return {"message": "Payment approval was cancelled. Nothing was held."}


@app.post("/items/{iid}/verify")   # temporary: the AI proof checker replaces this in Step 11
def verify(iid: int):
    with SessionLocal() as db:
        item = db.get(LineItem, iid)
        if not item:
            raise HTTPException(404, f"Item {iid} not found")
        item.status = "verified"
        log(db, item.pact_id, "item_verified", item.name)
        db.commit()
        return {"item": item.name, "status": item.status}


@app.post("/items/{iid}/capture")
def capture_item(iid: int):
    with SessionLocal() as db:
        item = db.get(LineItem, iid)
        if not item:
            raise HTTPException(404, f"Item {iid} not found")
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
        return {"item": item.name, "captured": paypal.usd(item.price_cents),
                "capture_id": r["id"]}


@app.post("/pacts/{pid}/close")
def close(pid: int):
    with SessionLocal() as db:
        pact = get_pact_or_404(db, pid)
        for auth_id in {i.auth_id for i in pact.items if i.auth_id}:
            code = paypal.void(auth_id)
            db.add(Payment(pact_id=pid, kind="void", paypal_id=auth_id, status=str(code)))
        for i in pact.items:
            if i.status in ("agreed", "verified"):
                i.status = "released"
        pact.status = "closed"
        log(db, pid, "pact_closed", "unused holds released")
        db.commit()
        return {"status": "closed"}


@app.get("/pacts/{pid}")
def get_pact(pid: int):
    with SessionLocal() as db:
        pact = get_pact_or_404(db, pid)
        return {
            "title": pact.title, "status": pact.status, "version": pact.version,
            "items": [{"id": i.id, "name": i.name, "price": paypal.usd(i.price_cents),
                       "version": i.version, "status": i.status,
                       "held": i.auth_id is not None} for i in pact.items],
            "payments": [{"kind": p.kind, "paypal_id": p.paypal_id,
                          "amount": paypal.usd(p.amount_cents)}
                         for p in db.query(Payment).filter_by(pact_id=pid)],
            "history": [f"{e.created_at:%H:%M:%S} {e.type}: {e.detail}"
                        for e in db.query(Event).filter_by(pact_id=pid)],
        }


# ---------------------------------------------------------------
# Rules, change requests, negotiation
# ---------------------------------------------------------------
@app.post("/pacts/{pid}/rules")
def set_rules(pid: int, r: RulesIn):
    with SessionLocal() as db:
        get_pact_or_404(db, pid)
        db.query(Rules).filter_by(pact_id=pid, side=r.side).delete()
        db.add(Rules(pact_id=pid, side=r.side, rules_json=json.dumps(r.rules)))
        log(db, pid, "rules_set", f"{r.side} rules saved")
        db.commit()
        return {"saved": r.side}


@app.post("/pacts/{pid}/changes")
def request_change(pid: int, c: ChangeIn):
    with SessionLocal() as db:
        pact = get_pact_or_404(db, pid)
        cr = ChangeRequest(pact_id=pid, item_name=c.item_name,
                           asked_cents=c.asked_cents, reason=c.reason)
        db.add(cr)
        db.flush()

        def turn(agent, msg):
            db.add(NegotiationTurn(change_id=cr.id, agent=agent, message=msg))

        turn("seller", f"Requesting '{c.item_name}' for {paypal.usd(c.asked_cents)}. {c.reason}")
        if c.seller_message:
            turn("seller_agent", c.seller_message)
        if c.buyer_message:
            turn("buyer_agent", c.buyer_message)

        # 1. Referee finds a price without revealing either limit
        result = referee.settle(c.buyer_max_cents, c.seller_min_cents, c.asked_cents)
        if not result["deal"]:
            cr.status = "no_deal"
            turn("referee", "No price satisfies both sides. Handing off to humans.")
            log(db, pid, "change_no_deal", c.item_name)
            db.commit()
            return {"change_id": cr.id, "status": cr.status}

        cr.agreed_cents = result["price_cents"]
        turn("referee", f"Deal possible at {paypal.usd(cr.agreed_cents)}.")

        # 2. Policy engine decides (AI proposes, code decides)
        decision, why = policy.check_change(get_rules(db, pid, "buyer"),
                                            current_total(pact), c.item_name,
                                            cr.agreed_cents)
        cr.decision_reason = why
        turn("policy", f"{decision.upper()}: {why}")

        if decision == "allow":
            cr.status = "approved"
            add_item_from_change(db, pact, cr)
        elif decision == "ask_human":
            cr.status = "awaiting_buyer"
        else:
            cr.status = "blocked"

        log(db, pid, f"change_{cr.status}", f"{c.item_name}: {why}")
        db.commit()
        return {"change_id": cr.id, "status": cr.status,
                "agreed_price": paypal.usd(cr.agreed_cents), "reason": why}


@app.post("/pacts/{pid}/changes/ai")
def ai_change(pid: int, body: AiChangeIn):
    with SessionLocal() as db:
        pact = get_pact_or_404(db, pid)
        summary = "; ".join(f"{i.name} {paypal.usd(i.price_cents)} ({i.status})"
                            for i in pact.items)
        buyer_rules = get_rules(db, pid, "buyer")
        seller_rules = get_rules(db, pid, "seller")

    try:
        req = agents.intake(body.message)                  # text -> structured request
        b = agents.buyer_agent(buyer_rules, summary, req)  # private max + message
        s = agents.seller_agent(seller_rules, req)         # private min + message
    except Exception as e:
        raise HTTPException(502, f"AI agent error: {type(e).__name__}: {e}")

    # Hand off to the same referee + policy engine. Limits are never returned.
    return request_change(pid, ChangeIn(
        item_name=req.item_name, asked_cents=req.asked_cents, reason=req.reason,
        buyer_max_cents=b.private_max_cents, seller_min_cents=s.private_min_cents,
        buyer_message=b.public_message, seller_message=s.public_message))


@app.post("/changes/{cid}/approve")
def approve_change(cid: int):
    with SessionLocal() as db:
        cr = db.get(ChangeRequest, cid)
        if not cr:
            raise HTTPException(404, f"Change {cid} not found")
        if cr.status != "awaiting_buyer":
            raise HTTPException(400, f"Change is '{cr.status}', not awaiting approval")
        pact = db.get(Pact, cr.pact_id)
        cr.status = "approved"
        db.add(NegotiationTurn(change_id=cid, agent="buyer", message="Approved by buyer"))
        add_item_from_change(db, pact, cr)
        db.commit()
        return {"status": "approved",
                "next": f"POST /pacts/{pact.id}/checkout to hold the new amount"}


@app.post("/changes/{cid}/decline")
def decline_change(cid: int):
    with SessionLocal() as db:
        cr = db.get(ChangeRequest, cid)
        if not cr:
            raise HTTPException(404, f"Change {cid} not found")
        cr.status = "declined"
        db.add(NegotiationTurn(change_id=cid, agent="buyer", message="Declined by buyer"))
        log(db, cr.pact_id, "change_declined", cr.item_name)
        db.commit()
        return {"status": "declined"}


@app.get("/changes/{cid}")
def get_change(cid: int):
    with SessionLocal() as db:
        cr = db.get(ChangeRequest, cid)
        if not cr:
            raise HTTPException(404, f"Change {cid} not found")
        turns = db.query(NegotiationTurn).filter_by(change_id=cid)
        return {"item": cr.item_name, "status": cr.status,
                "asked": paypal.usd(cr.asked_cents),
                "agreed": paypal.usd(cr.agreed_cents) if cr.agreed_cents else None,
                "reason": cr.decision_reason,
                "transcript": [f"{t.agent}: {t.message}" for t in turns]}