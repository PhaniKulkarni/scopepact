from datetime import datetime
from sqlalchemy import create_engine, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

engine = create_engine("sqlite:///./scopepact.db",
                       connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    pass

class Pact(Base):
    __tablename__ = "pacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    status: Mapped[str] = mapped_column(default="draft")   # draft, active, closed
    version: Mapped[int] = mapped_column(default=1)
    items = relationship("LineItem", back_populates="pact")

class LineItem(Base):
    __tablename__ = "line_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    pact_id: Mapped[int] = mapped_column(ForeignKey("pacts.id"))
    name: Mapped[str]
    price_cents: Mapped[int]
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="agreed")  # agreed, verified, paid, blocked
    auth_id: Mapped[str | None] = mapped_column(default=None)
    criteria: Mapped[str] = mapped_column(default="")
    pact = relationship("Pact", back_populates="items")

class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    pact_id: Mapped[int]
    item_id: Mapped[int | None] = mapped_column(default=None)
    kind: Mapped[str]            # order, authorization, capture, void
    paypal_id: Mapped[str]
    amount_cents: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="")

class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    pact_id: Mapped[int]
    type: Mapped[str]
    detail: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

class Rules(Base):
    __tablename__ = "rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    pact_id: Mapped[int]
    side: Mapped[str]            # buyer or seller
    rules_json: Mapped[str]

class ChangeRequest(Base):
    __tablename__ = "change_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    pact_id: Mapped[int]
    item_name: Mapped[str]
    reason: Mapped[str] = mapped_column(default="")
    asked_cents: Mapped[int]
    agreed_cents: Mapped[int | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default="open")
    # open, approved, awaiting_buyer, blocked, no_deal, declined
    decision_reason: Mapped[str] = mapped_column(default="")
    criteria: Mapped[str] = mapped_column(default="")

class NegotiationTurn(Base):
    __tablename__ = "negotiation_turns"
    id: Mapped[int] = mapped_column(primary_key=True)
    change_id: Mapped[int]
    agent: Mapped[str]           # buyer_agent, seller_agent, referee, policy
    message: Mapped[str]

class Proof(Base):
    __tablename__ = "proofs"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int]
    filename: Mapped[str]
    file_hash: Mapped[str]
    passed: Mapped[bool]
    confidence: Mapped[float]
    reasons: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

Base.metadata.create_all(engine)