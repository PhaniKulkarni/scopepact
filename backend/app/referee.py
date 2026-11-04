def settle(buyer_max_cents, seller_min_cents, asked_cents):
    """Neither side sees the other's limit. Returns a fair price or no deal."""
    if buyer_max_cents < seller_min_cents:
        return {"deal": False}
    price = round((buyer_max_cents + seller_min_cents) / 2)
    price = min(price, asked_cents)        # never above what the seller asked
    return {"deal": True, "price_cents": price}