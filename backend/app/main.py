import os
import json
import hashlib
import uuid
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from app.models import (SessionLocal, Pact, LineItem, Payment, Event,
                        Rules, ChangeRequest, NegotiationTurn, Proof)
from app import paypal, policy, referee, agents

app = FastAPI(title="ScopePact")
API = os.getenv("PUBLIC_URL", "http://localhost:8000")

ALLOWED_IMAGES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MIN_CONFIDENCE = 0.8

# The web screen lives in backend/static and is served at /app
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
app.mount("/app", StaticFiles(directory=STATIC_DIR, html=True), name="app")


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/app/")


# ---------------------------------------------------------------
# Input shapes
# ---------------------------------------------------------------
class ItemIn(BaseModel):
    name: str
    price_cents: int
    criteria: str = ""       # what "done" means; the proof checker uses this


class PactIn(BaseModel):
    title: str
    items: list[ItemIn]
    buyer_rules: dict = {}   # optional: client's rules, saved with the pact
    seller_rules: dict = {}  # optional: freelancer's rules


class RulesIn(BaseModel):
    side: str                # buyer or seller
    rules: dict


class ChangeIn(BaseModel):
    item_name: str
    asked_cents: int
    reason: str = ""
    buyer_max_cents: int     # produced by the buyer AI agent (or typed for testing)
    seller_min_cents: int    # produced by the seller AI agent (or typed for testing)
    buyer_message: str = ""
    seller_message: str = ""
    criteria: str = ""       # what the finished work must show (for the proof checker)


class AiChangeIn(BaseModel):
    message: str             # the seller's casual text


class ReviewIn(BaseModel):
    approve: bool            # client's decision on a proof the AI wasn't sure about


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
    db.add(LineItem(pact_id=pact.id, name=cr.item_name, price_cents=cr.agreed_cents,
                    version=pact.version, criteria=cr.criteria or cr.reason))
    log(db, pact.id, "pact_version",
        f"v{pact.version}: added {cr.item_name} at {paypal.usd(cr.agreed_cents)}")


def get_pact_or_404(db, pid):
    pact = db.get(Pact, pid)
    if not pact:
        raise HTTPException(404, f"Pact {pid} not found")
    return pact


# ---------------------------------------------------------------
# Demo helper: one click creates a ready-to-demo pact with rules
# ---------------------------------------------------------------
@app.post("/demo/seed")
def demo_seed():
    with SessionLocal() as db:
        pact = Pact(title="Brightside Bakery website")
        db.add(pact)
        db.flush()
        db.add(LineItem(pact_id=pact.id, name="Homepage design", price_cents=300,
                        criteria="A desktop homepage layout with a header, navigation "
                                 "menu and at least one content section"))
        db.add(Rules(pact_id=pact.id, side="buyer", rules_json=json.dumps({
            "budget_cap_cents": 500, "auto_approve_under_cents": 150,
            "never_approve": ["rush"], "notes": "Small business, price-sensitive"})))
        db.add(Rules(pact_id=pact.id, side="seller", rules_json=json.dumps({
            "floor_cents": 200,
            "notes": "Mobile layouts take about 4 hours; first 2 revisions free"})))
        log(db, pact.id, "pact_created", "Demo pact with buyer and seller rules")
        db.commit()
        return {"pact_id": pact.id}


# ---------------------------------------------------------------
# Pacts and payments
# ---------------------------------------------------------------
@app.get("/pacts")
def list_pacts():
    with SessionLocal() as db:
        return [{"id": p.id, "title": p.title, "status": p.status, "version": p.version,
                 "agreed": paypal.usd(current_total(p)),
                 "paid": paypal.usd(sum(i.price_cents for i in p.items if i.status == "paid"))}
                for p in db.query(Pact).order_by(Pact.id.desc())]


@app.post("/pacts")
def create_pact(p: PactIn):
    with SessionLocal() as db:
        pact = Pact(title=p.title)
        db.add(pact)
        db.flush()
        for i in p.items:
            db.add(LineItem(pact_id=pact.id, name=i.name, price_cents=i.price_cents,
                            criteria=i.criteria))
        for side, rules in (("buyer", p.buyer_rules), ("seller", p.seller_rules)):
            if rules:
                db.add(Rules(pact_id=pact.id, side=side, rules_json=json.dumps(rules)))
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
            f"{API}/paypal/return?pact_id={pid}", f"{API}/paypal/cancel?pact_id={pid}")
        db.add(Payment(pact_id=pid, kind="order", paypal_id=order_id, amount_cents=total))
        log(db, pid, "order_created", f"{order_id} for {paypal.usd(total)}")
        db.commit()
        return {"approve_link": link, "amount": paypal.usd(total)}


@app.get("/paypal/return")
def paypal_return(token: str, pact_id: int):
    try:
        auth_id = paypal.authorize_order(token)
    except Exception:
        return RedirectResponse(f"/app/?pact={pact_id}&msg=auth_failed")
    with SessionLocal() as db:
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
    return RedirectResponse(f"/app/?pact={pact_id}&msg=held")


@app.get("/paypal/cancel")
def paypal_cancel(pact_id: int = 0):
    return RedirectResponse(f"/app/?pact={pact_id}&msg=cancelled")


@app.post("/items/{iid}/capture")
def capture_item(iid: int):
    with SessionLocal() as db:
        item = db.get(LineItem, iid)
        if not item:
            raise HTTPException(404, f"Item {iid} not found")
        # Policy rule: only items verified by the proof checker, with a PayPal hold, can be paid
        if item.status != "verified":
            raise HTTPException(400, f"Blocked: item is '{item.status}', not verified")
        if not item.auth_id:
            raise HTTPException(400, "Blocked: no PayPal hold for this item yet")
        r = paypal.capture(item.auth_id, item.price_cents,
                           f"pact-{item.pact_id}-item-{item.id}-{uuid.uuid4().hex[:8]}",
                           f"{item.name} verified")
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
        for auth_id in {i.auth_id for i in pact.items
                        if i.auth_id and i.status in ("agreed", "verified", "needs_review", "released")}:
            code = paypal.void(auth_id)
            db.add(Payment(pact_id=pid, kind="void", paypal_id=auth_id, status=str(code)))
        for i in pact.items:
            if i.status in ("agreed", "verified", "needs_review"):
                i.status = "released"
        pact.status = "closed"
        log(db, pid, "pact_closed", "unused holds released")
        db.commit()
        return {"status": "closed"}


@app.get("/pacts/{pid}")
def get_pact(pid: int):
    with SessionLocal() as db:
        pact = get_pact_or_404(db, pid)
        item_ids = [i.id for i in pact.items]
        held = sum(i.price_cents for i in pact.items
                   if i.auth_id and i.status in ("agreed", "verified", "needs_review"))
        paid = sum(i.price_cents for i in pact.items if i.status == "paid")
        unheld = sum(i.price_cents for i in pact.items
                     if i.auth_id is None and i.status == "agreed")
        return {
            "id": pact.id, "title": pact.title, "status": pact.status,
            "version": pact.version,
            "totals": {"agreed": paypal.usd(current_total(pact)), "held": paypal.usd(held),
                       "paid": paypal.usd(paid), "unheld": paypal.usd(unheld)},
            "rules": {"buyer": get_rules(db, pid, "buyer"),
                      "seller": get_rules(db, pid, "seller")},
            "items": [{"id": i.id, "name": i.name, "price": paypal.usd(i.price_cents),
                       "version": i.version, "status": i.status, "criteria": i.criteria,
                       "held": i.auth_id is not None} for i in pact.items],
            "changes": [{"id": c.id, "item": c.item_name, "status": c.status,
                         "asked": paypal.usd(c.asked_cents),
                         "agreed": paypal.usd(c.agreed_cents) if c.agreed_cents else None,
                         "reason": c.decision_reason}
                        for c in db.query(ChangeRequest).filter_by(pact_id=pid)
                                                        .order_by(ChangeRequest.id)],
            "proofs": [{"item_id": p.item_id, "file": p.filename, "passed": p.passed,
                        "confidence": p.confidence, "reasons": json.loads(p.reasons)}
                       for p in db.query(Proof).filter(Proof.item_id.in_(item_ids))],
            "payments": [{"kind": p.kind, "paypal_id": p.paypal_id,
                          "amount": paypal.usd(p.amount_cents)}
                         for p in db.query(Payment).filter_by(pact_id=pid)],
            "history": [f"{e.created_at:%H:%M:%S} {e.type}: {e.detail}"
                        for e in db.query(Event).filter_by(pact_id=pid)],
            "events": [{"time": e.created_at.isoformat() + "Z", "type": e.type, "detail": e.detail}
                       for e in db.query(Event).filter_by(pact_id=pid).order_by(Event.id)],
        }


# ---------------------------------------------------------------
# Proof of work (AI proof checker)
# ---------------------------------------------------------------
@app.post("/items/{iid}/proof")
async def submit_proof(iid: int, file: UploadFile = File(...)):
    if file.content_type not in ALLOWED_IMAGES:
        raise HTTPException(400, "Upload a PNG, JPG, WEBP or GIF image")
    data = await file.read()
    file_hash = hashlib.sha256(data).hexdigest()

    with SessionLocal() as db:
        item = db.get(LineItem, iid)
        if not item:
            raise HTTPException(404, f"Item {iid} not found")
        if item.status not in ("agreed", "needs_review"):
            raise HTTPException(400, f"Item is '{item.status}', proof not accepted")

        # Anti-fraud: the same image can't prove two different items
        reused = db.query(Proof).filter(Proof.file_hash == file_hash,
                                        Proof.item_id != iid).first()
        if reused:
            log(db, item.pact_id, "proof_rejected", f"{item.name}: reused image")
            db.commit()
            raise HTTPException(400, "Rejected: this image was already used as proof for another item")

        name, criteria, pact_id = item.name, item.criteria, item.pact_id

    # AI check runs outside the database session (it can take a few seconds)
    try:
        result = agents.check_proof(name, criteria, data, file.content_type)
    except Exception as e:
        raise HTTPException(502, f"AI proof check error: {type(e).__name__}: {e}")

    with SessionLocal() as db:
        item = db.get(LineItem, iid)
        db.add(Proof(item_id=iid, filename=file.filename or "upload", file_hash=file_hash,
                     passed=result.passed, confidence=result.confidence,
                     reasons=json.dumps(result.reasons)))
        if result.passed and result.confidence >= MIN_CONFIDENCE:
            item.status = "verified"
            verdict = "verified"
        elif result.passed:
            item.status = "needs_review"      # passed, but AI not confident enough
            verdict = "needs_review"
        else:
            verdict = "failed"
        log(db, pact_id, f"proof_{verdict}",
            f"{name} ({result.confidence:.2f}): {'; '.join(result.reasons)}")
        db.commit()
        return {"item": name, "verdict": verdict,
                "confidence": result.confidence, "reasons": result.reasons,
                "next": f"POST /items/{iid}/capture" if verdict == "verified"
                        else "Upload better proof"}


@app.post("/items/{iid}/review")
def review_proof(iid: int, body: ReviewIn):
    """Human in the loop: the client decides when the AI wasn't confident enough."""
    with SessionLocal() as db:
        item = db.get(LineItem, iid)
        if not item:
            raise HTTPException(404, f"Item {iid} not found")
        rejected_before = item.status == "agreed" and db.query(Proof).filter_by(item_id=iid).first()
        if item.status != "needs_review" and not (body.approve and rejected_before):
            raise HTTPException(400, f"Item is '{item.status}', nothing to review")
        item.status = "verified" if body.approve else "agreed"
        log(db, item.pact_id, "human_review",
            f"{item.name}: client {'approved' if body.approve else 'rejected'} the proof")
        db.commit()
        return {"item": item.name, "status": item.status}


@app.post("/items/{iid}/remove")
def remove_item(iid: int):
    """Client drops an unpaid item from the agreement. Its hold is released when the pact closes."""
    with SessionLocal() as db:
        item = db.get(LineItem, iid)
        if not item:
            raise HTTPException(404, f"Item {iid} not found")
        if item.status not in ("agreed", "verified", "needs_review"):
            raise HTTPException(400, f"Item is '{item.status}', it can't be removed")
        item.status = "released"
        log(db, item.pact_id, "item_removed", f"{item.name} {paypal.usd(item.price_cents)}")
        db.commit()
        return {"item": item.name, "status": "released"}


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
                           asked_cents=c.asked_cents, reason=c.reason,
                           criteria=c.criteria or c.reason)
        db.add(cr)
        db.flush()

        def turn(agent, msg):
            db.add(NegotiationTurn(change_id=cr.id, agent=agent, message=msg))

        turn("seller", f"Requesting '{c.item_name}' for ${paypal.usd(c.asked_cents)}. {c.reason}")
        if c.seller_message:
            turn("seller_agent", c.seller_message)
        if c.buyer_message:
            turn("buyer_agent", c.buyer_message)

        # 1. Referee finds a price without revealing either limit
        result = referee.settle(c.buyer_max_cents, c.seller_min_cents, c.asked_cents)
        if not result["deal"]:
            cr.status = "no_deal"
            turn("referee", "Sealed limits don't overlap. Handing off to humans.")
            log(db, pid, "change_no_deal", c.item_name)
            db.commit()
            return {"change_id": cr.id, "status": cr.status}

        cr.agreed_cents = result["price_cents"]
        turn("referee", f"Both sealed limits overlap. Fair price: ${paypal.usd(cr.agreed_cents)}.")

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
        criteria=req.criteria, buyer_max_cents=b.private_max_cents, seller_min_cents=s.private_min_cents,
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


@app.post("/changes/{cid}/accept-ask")
def accept_asked_price(cid: int):
    """Agents found no fair price: the client chooses to accept the freelancer's asked price."""
    with SessionLocal() as db:
        cr = db.get(ChangeRequest, cid)
        if not cr:
            raise HTTPException(404, f"Change {cid} not found")
        if cr.status != "no_deal":
            raise HTTPException(400, f"Change is '{cr.status}', not waiting for a human decision")
        pact = db.get(Pact, cr.pact_id)
        cr.agreed_cents = cr.asked_cents
        cr.status = "approved"
        cr.decision_reason = "Client accepted the freelancer's price"
        db.add(NegotiationTurn(change_id=cid, agent="buyer",
                               message=f"Client accepted the freelancer's price of ${paypal.usd(cr.asked_cents)}"))
        log(db, pact.id, "change_accepted_by_client", f"{cr.item_name} at {paypal.usd(cr.asked_cents)}")
        add_item_from_change(db, pact, cr)
        db.commit()
        return {"status": "approved", "agreed_price": paypal.usd(cr.agreed_cents),
                "next": f"POST /pacts/{pact.id}/checkout to hold the new amount"}


@app.post("/changes/{cid}/decline")
def decline_change(cid: int):
    with SessionLocal() as db:
        cr = db.get(ChangeRequest, cid)
        if not cr:
            raise HTTPException(404, f"Change {cid} not found")
        if cr.status not in ("awaiting_buyer", "no_deal"):
            raise HTTPException(400, f"Change is '{cr.status}', nothing to decline")
        cr.status = "declined"
        db.add(NegotiationTurn(change_id=cid, agent="buyer", message="Declined by the client"))
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
                "transcript": [{"agent": t.agent, "message": t.message} for t in turns]}