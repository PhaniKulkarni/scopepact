def check_change(buyer_rules, current_total_cents, item_name, price_cents):
    """Returns (decision, reason). decision: allow, ask_human, block"""
    for banned in buyer_rules.get("never_approve", []):
        if banned.lower() in item_name.lower():
            return "block", f"'{item_name}' matches a banned item: {banned}"

    new_total = current_total_cents + price_cents
    cap = buyer_rules.get("budget_cap_cents")
    if cap is not None and new_total > cap:
        over = new_total - cap
        return "ask_human", f"New total ${new_total/100:.2f} is ${over/100:.2f} over the budget cap"

    if price_cents <= buyer_rules.get("auto_approve_under_cents", 0):
        return "allow", "Within auto-approve limit and budget"

    return "ask_human", "Above the auto-approve limit"