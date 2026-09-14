"""
Routes an inbound /v1/reply turn to one of: send | wait | end.

Order of checks (first match wins), mirroring the open challenges in
challenge-brief.md §12 and the replay scenarios in challenge-testing-brief.md
§4 / examples/api-call-examples.md §4:

  1. Auto-reply detection      — canned-phrase match or repeated-verbatim reply.
     1st occurrence -> nudge once. 2nd -> wait. 3rd+ -> end.
  2. Hard opt-out / hostility  -> end immediately, suppress the merchant.
  3. Explicit intent commitment -> flip conversation to "action" mode and
     respond with a concrete next step (no further qualifying questions).
  4. Off-topic / out-of-scope ask -> politely decline, redirect back on-topic.
  5. Otherwise: an engaged, on-topic reply -> compose a grounded follow-on.
"""

from __future__ import annotations

from typing import Any, Optional

from composer import (
    _AUTO_REPLY_PATTERNS, _OPT_OUT_PATTERNS, _HOSTILE_PATTERNS,
    _INTENT_PATTERNS, _OFF_TOPIC_PATTERNS,
    _is_hinglish, _owner_or_name, _strip_urls, _dedupe_against,
)
from store import ContextStore, ConversationStore, ConversationState


def _matches_any(patterns, text: str) -> bool:
    return any(p.search(text) for p in patterns)


def handle_reply(ctx_store: ContextStore, conv_store: ConversationStore, req: dict[str, Any]) -> dict[str, Any]:
    conversation_id = req["conversation_id"]
    merchant_id = req.get("merchant_id")
    customer_id = req.get("customer_id")
    message = (req.get("message") or "").strip()

    cs = conv_store.get_or_create_for_reply(conversation_id, merchant_id, customer_id)
    cs.turn += 1
    cs.merchant_messages.append(message)

    merchant = ctx_store.get("merchant", cs.merchant_id) if cs.merchant_id else None
    customer = ctx_store.get("customer", cs.customer_id) if cs.customer_id else None

    # --- 1. Auto-reply detection ------------------------------------------------
    is_canned = _matches_any(_AUTO_REPLY_PATTERNS, message)
    is_repeat = (
        len(cs.merchant_messages) >= 2
        and cs.merchant_messages[-1].strip().lower() == cs.merchant_messages[-2].strip().lower()
    )
    if is_canned or is_repeat:
        cs.auto_reply_streak += 1
    else:
        cs.auto_reply_streak = 0

    if cs.auto_reply_streak == 1:
        owner = _owner_or_name(merchant) if merchant else "the owner"
        body = _strip_urls(f"Looks like an auto-reply 😊 When {owner} sees this, just reply 'Yes' and I'll pick up where we left off.")
        cs.sent_bodies.append(body)
        return {"action": "send", "body": body, "cta": "binary_yes_no",
                "rationale": "Detected merchant auto-reply (canned phrasing or verbatim repeat); one explicit prompt to flag for the owner."}
    if cs.auto_reply_streak == 2:
        return {"action": "wait", "wait_seconds": 86400,
                "rationale": "Same auto-reply twice in a row — owner likely not at phone. Waiting 24h before retry."}
    if cs.auto_reply_streak >= 3:
        cs.status = "ended"
        return {"action": "end",
                "rationale": "Auto-reply 3x in a row, zero real engagement signal. Closing conversation."}

    # --- 2. Hard opt-out / hostility --------------------------------------------
    if _matches_any(_OPT_OUT_PATTERNS, message):
        cs.status = "ended"
        conv_store.suppress_merchant(cs.merchant_id)
        return {"action": "end",
                "rationale": "Explicit opt-out signal. Closing conversation; suppressing this merchant for the remainder of the test."}
    if _matches_any(_HOSTILE_PATTERNS, message):
        cs.status = "ended"
        conv_store.suppress_merchant(cs.merchant_id)
        return {"action": "end",
                "rationale": "Hostility detected. Closing without further engagement; suppressing this merchant going forward."}

    # --- 3. Explicit intent transition ------------------------------------------
    if _matches_any(_INTENT_PATTERNS, message) and cs.mode != "action":
        cs.mode = "action"
        body = _strip_urls(_action_step_body(merchant, customer, cs))
        body = _dedupe_against(body, cs.sent_bodies, {})
        cs.sent_bodies.append(body)
        return {"action": "send", "body": body, "cta": "binary_confirm_cancel",
                "rationale": "Merchant explicitly committed; switching from pitch to action mode instead of asking another qualifying question."}

    # --- 4. Off-topic / out-of-scope ask -----------------------------------------
    if _matches_any(_OFF_TOPIC_PATTERNS, message):
        body = _strip_urls("That's outside what I can help with directly — best to check with your CA/local office for that. " + _back_on_topic(cs))
        body = _dedupe_against(body, cs.sent_bodies, {})
        cs.sent_bodies.append(body)
        return {"action": "send", "body": body, "cta": "open_ended",
                "rationale": "Out-of-scope ask politely declined; redirected back to the original thread without losing it."}

    # --- 5. Engaged, on-topic reply -----------------------------------------------
    body = _strip_urls(_engaged_followup(merchant, customer, cs, message))
    body = _dedupe_against(body, cs.sent_bodies, {})
    cs.sent_bodies.append(body)
    return {"action": "send", "body": body, "cta": "open_ended",
            "rationale": "Merchant engaged on-topic; acknowledging + advancing with the next concrete step."}


def _action_step_body(merchant: Optional[dict], customer: Optional[dict], cs: ConversationState) -> str:
    name = _owner_or_name(merchant) if merchant else "there"
    hinglish = _is_hinglish(merchant or {}, customer)
    if hinglish:
        return (f"{name}, badhiya. Proceeding now — drafting the next step, ready in a minute. "
                f"Reply CONFIRM to send it, or CANCEL to hold.")
    return (f"{name}, great — proceeding now. Drafting the next step, ready shortly. "
            f"Reply CONFIRM to send it, or CANCEL to hold.")


def _back_on_topic(cs: ConversationState) -> str:
    if cs.sent_bodies:
        return "Coming back to what we were discussing — want me to continue where we left off?"
    return "Happy to help with what Vera actually covers — want me to continue?"


def _engaged_followup(merchant: Optional[dict], customer: Optional[dict], cs: ConversationState, message: str) -> str:
    name = _owner_or_name(merchant) if merchant else "there"
    hinglish = _is_hinglish(merchant or {}, customer)
    lowered = message.lower()
    affirmative = any(w in lowered for w in ("yes", "please", "send", "ok", "sure", "haan", "theek"))
    if affirmative:
        if hinglish:
            return f"{name}, done — sending it now. Bata dena agar kuch aur chahiye."
        return f"{name}, sending that now. Let me know if you'd like anything adjusted."
    return f"{name}, noted. What would be most useful next — should I go ahead, or do you have a question first?"
