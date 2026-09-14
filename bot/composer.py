"""
The composition brain: compose(category, merchant, trigger, customer?) -> message,
plus the trigger-selection ("decision quality") logic and the reply router.

Two composition modes, chosen by COMPOSER_MODE env var:
  - "template" (default): deterministic, hand-written per-trigger-kind templates
    grounded strictly in fields actually present in the pushed contexts. Zero
    external calls, zero API key required, always deterministic.
  - "llm": builds a structured prompt from the same 4 contexts and asks an LLM
    (temperature=0) to compose. Falls back to "template" mode on any failure
    (bad JSON, timeout, missing key) so a tick never goes empty because of an
    LLM hiccup.

Design choices driven directly by challenge-brief.md / challenge-testing-brief.md:
  - Never fabricate a number/date/name that isn't in the context (§5.8, penalty
    in api-call-examples.md and case-studies.md #2).
  - Exactly one CTA, no URLs (api-call-examples.md Example F.4).
  - Honor merchant/customer language preference (hi-en code-mix when present).
  - Prefer service+price offers over generic discounts (§10).
"""

from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from typing import Any, Optional
from urllib import request as urlrequest

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COMPOSER_MODE = os.environ.get("COMPOSER_MODE", "template")  # "template" | "llm"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-opus-4-1-20250805")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT_SECONDS", "20"))


def model_name() -> str:
    if COMPOSER_MODE == "llm" and LLM_API_KEY:
        return f"{LLM_PROVIDER}:{LLM_MODEL}"
    return "template-composer-v1 (deterministic, no external calls)"


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"[ \t]+")

_QUALIFYING_PHRASES = ("would you", "do you", "can you tell", "what if", "how about")
_ACTIONING_WORDS = ("done", "sending", "draft", "here", "confirm", "proceed", "next")

_AUTO_REPLY_PATTERNS = [
    re.compile(p, re.I) for p in [
        r"thank you for contact",
        r"we (will|shall|'ll) (respond|get back|reply)",
        r"team will (respond|get back|reply)",
        r"automated (assistant|reply|response|message)",
        r"currently (unavailable|away|closed)",
        r"busy right now",
        r"out of office",
    ]
]

_OPT_OUT_PATTERNS = [
    re.compile(p, re.I) for p in [
        r"not interested",
        r"stop messag",
        r"unsubscribe",
        r"don.?t (contact|message|text) me",
        r"leave me alone",
        r"\bstop\b",
    ]
]

_HOSTILE_PATTERNS = [
    re.compile(p, re.I) for p in [
        r"\b(useless|spam|scam|nonsense|nuisance|harass)\b",
        r"shut up",
        r"stfu",
        r"stupid|idiot",
        r"why are you bothering",
        r"f+u+c+k",
    ]
]

_INTENT_PATTERNS = [
    re.compile(p, re.I) for p in [
        r"let.?s do it",
        r"ok(ay)?[, ]+let.?s",
        r"go ahead",
        r"sounds good",
        r"\bproceed\b",
        r"\bconfirm\b",
        r"yes.*(join|start|send it|do it)",
        r"i want to join",
        r"count me in",
        r"mujhe.*(jud|jod|shamil)",   # "Mujhe magicpin judrna hai" style Hindi intent
    ]
]

_OFF_TOPIC_PATTERNS = [
    re.compile(p, re.I) for p in [
        r"\bgst\b", r"\bincome tax\b", r"\bloan\b", r"\binsurance\b",
        r"\blicense renewal\b", r"\belectricity bill\b",
    ]
]


def _strip_urls(body: str) -> str:
    body = _URL_RE.sub("", body)
    return _WS_RE.sub(" ", body).strip()


def _owner_or_name(merchant: dict[str, Any]) -> str:
    ident = merchant.get("identity", {}) or {}
    return ident.get("owner_first_name") or ident.get("name") or "there"


def _salutation(category: dict[str, Any], merchant: dict[str, Any]) -> str:
    ident = merchant.get("identity", {}) or {}
    first = ident.get("owner_first_name", "")
    examples = ((category.get("voice") or {}).get("salutation_examples")) or []
    for ex in examples:
        if "{first_name}" in ex and first:
            return ex.replace("{first_name}", first)
    return first or ident.get("name", "there")


def _is_hinglish(merchant: dict[str, Any], customer: Optional[dict[str, Any]] = None) -> bool:
    if customer:
        pref = ((customer.get("identity") or {}).get("language_pref") or "").lower()
        if "hi" in pref:
            return True
    langs = (merchant.get("identity", {}) or {}).get("languages", []) or []
    return "hi" in langs


def _want_me_to(hinglish: bool) -> str:
    return "Chahiye toh main" if hinglish else "Want me to"


def _active_offers(merchant: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in merchant.get("offers", []) or [] if o.get("status") == "active"]


def _best_offer_for(merchant: dict[str, Any], keyword: Optional[str] = None) -> Optional[dict[str, Any]]:
    offers = _active_offers(merchant)
    if not offers:
        return None
    if keyword:
        kw = keyword.lower()
        for o in offers:
            if kw in (o.get("title") or "").lower():
                return o
    return offers[0]


def _digest_item(category: dict[str, Any], item_id: Optional[str]) -> Optional[dict[str, Any]]:
    digest = category.get("digest", []) or []
    if item_id:
        for d in digest:
            if d.get("id") == item_id:
                return d
    return digest[0] if digest else None


def _peer_stats(category: dict[str, Any]) -> dict[str, Any]:
    return category.get("peer_stats", {}) or {}


def _pct(x: Any, digits: int = 1) -> str:
    try:
        return f"{float(x) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return str(x)


def _signed_pct(x: Any, digits: int = 0) -> str:
    try:
        v = float(x) * 100
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.{digits}f}%"
    except (TypeError, ValueError):
        return str(x)


def _months_between(iso_a: Optional[str], iso_b: Optional[str]) -> Optional[int]:
    if not iso_a or not iso_b:
        return None
    try:
        from datetime import date
        a = date.fromisoformat(iso_a[:10])
        b = date.fromisoformat(iso_b[:10])
        days = abs((b - a).days)
        return max(0, round(days / 30.44))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-trigger-kind handlers.
#
# Each returns a dict: {lines, cta_type, cta_text, send_as, why}
#   lines     — list[str] of body sentences (joined with spaces)
#   cta_type  — "open_ended" | "binary_yes_no" | "binary_confirm_cancel" |
#               "multi_choice_slot" | "none"
#   cta_text  — the actual last-sentence CTA
#   send_as   — "vera" | "merchant_on_behalf"
#   why       — short rationale fragment (what fact this is grounded on)
#
# Handlers must never invent a fact absent from the inputs — if a field they'd
# want isn't there, they either omit that clause or fall through to the
# generic handler at the bottom of the file.
# ---------------------------------------------------------------------------

Ctx = dict[str, Any]


def _h_research_digest(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    item = _digest_item(category, payload.get("top_item_id"))
    hinglish = _is_hinglish(merchant)
    if not item:
        return _h_generic(category, merchant, trigger, customer)
    src = item.get("source") or "This week's digest"
    lines = [f"{src} landed."]

    fact = item.get("summary") or item.get("title")
    if item.get("trial_n"):
        fact = f"{item['trial_n']:,}-sample study: {item.get('title')}"
    lines.append(fact + ("." if not fact.endswith(".") else ""))
    segment = item.get("patient_segment", "")
    signals = merchant.get("signals", []) or []
    if segment and segment.replace("_", "_cohort") in " ".join(signals):
        lines.append(f"Directly relevant to your {segment.replace('_', ' ')} patients.")
    elif item.get("actionable"):
        lines.append(item["actionable"] + ".")
    cta = f"{_want_me_to(hinglish)} pull the full abstract and draft a patient-ed message you can share?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"digest item '{item.get('id')}' from category.digest",
    }


def _h_regulation_change(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    item = _digest_item(category, payload.get("top_item_id"))
    if not item:
        return _h_generic(category, merchant, trigger, customer)
    hinglish = _is_hinglish(merchant)
    deadline = payload.get("deadline_iso", "")
    title = item.get("title", "")
    lines = [f"Compliance update: {title}."]
    if deadline and deadline not in title:
        lines.append(f"Effective {deadline}.")
    if item.get("summary"):
        lines.append(item["summary"])
    cta = f"{_want_me_to(hinglish)} draft a one-line SOP note for your team before the deadline?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"regulation digest item '{item.get('id')}', deadline {deadline}",
    }


def _h_recall_due(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    if not customer:
        return _h_generic(category, merchant, trigger, customer)
    payload = trigger.get("payload", {}) or {}
    hinglish = _is_hinglish(merchant, customer)
    cust_name = (customer.get("identity", {}) or {}).get("name", "there")
    merchant_name = (merchant.get("identity", {}) or {}).get("name", "the clinic")
    months = _months_between(payload.get("last_service_date"), None)
    last_visit = (customer.get("relationship", {}) or {}).get("last_visit")
    months = _months_between(last_visit, payload.get("due_date")) or months

    lines = [f"{merchant_name} here."]
    service = (payload.get("service_due") or "").replace("_", " ")
    if months:
        lines.append(f"It's been about {months} months since your last visit — your {service} recall is due.")
    elif service:
        lines.append(f"Your {service} recall is due.")

    slots = payload.get("available_slots") or []
    slot_labels = [s.get("label") for s in slots if s.get("label")]
    offer = _best_offer_for(merchant, service.split()[0] if service else None)
    if slot_labels:
        joiner = " ya " if hinglish else " or "
        noun = "slot" if len(slot_labels) == 1 else "slots"
        if hinglish:
            lines.append(f"Apke liye {len(slot_labels)} {noun} ready hain: {joiner.join(slot_labels)}.")
        else:
            lines.append(f"{len(slot_labels)} {noun} open: {joiner.join(slot_labels)}.")
    if offer:
        lines.append(f"{offer.get('title')}.")

    if len(slot_labels) >= 2:
        cta = "Reply 1 for the first slot, 2 for the second, or tell us a time that works."
        cta_type = "multi_choice_slot"
    else:
        cta = "Reply YES to book, or tell us a time that works."
        cta_type = "binary_yes_no"

    return {
        "lines": lines, "cta_type": cta_type, "cta_text": cta,
        "send_as": "merchant_on_behalf",
        "why": f"recall_due for {cust_name}: {months}mo since last visit, {len(slot_labels)} open slots",
    }


def _h_perf_dip(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    metric = payload.get("metric", "performance")
    delta = payload.get("delta_pct")
    window = payload.get("window", "week")
    hinglish = _is_hinglish(merchant)

    # Decision-quality nuance: check if a seasonal_beat explains this away
    # before framing it as a problem — mirrors case-studies.md #7 (gym
    # seasonal dip reframe), but grounded only in fields actually present.
    seasonal_note = None
    for beat in category.get("seasonal_beats", []) or []:
        note = (beat.get("note") or "").lower()
        if metric in note or "dip" in note or "lull" in note:
            seasonal_note = beat.get("note")
            break

    lines = []
    if delta is not None:
        lines.append(f"Your {metric} are {_signed_pct(delta)} this {window}.")
    else:
        lines.append(f"Your {metric} dipped this {window}.")

    if seasonal_note:
        lines.append(f"Worth flagging this may just be the seasonal pattern: {seasonal_note}.")
        cta = f"{_want_me_to(hinglish)} hold off on ad spend and focus on retention instead?"
    else:
        avg_key = f"avg_{metric}_30d"
        peer = _peer_stats(category).get(avg_key)
        if peer:
            lines.append(f"Category median is ~{peer}/30d for reference.")
        cta = f"{_want_me_to(hinglish)} look at what changed and suggest one fix?"

    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"perf_dip metric={metric} delta={delta}" + (" (seasonal-explained)" if seasonal_note else ""),
    }


def _h_perf_spike(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    metric = payload.get("metric", "performance")
    delta = payload.get("delta_pct")
    window = payload.get("window", "week")
    hinglish = _is_hinglish(merchant)
    driver = payload.get("likely_driver")
    lines = []
    if delta is not None:
        lines.append(f"Nice — your {metric} are {_signed_pct(delta)} this {window}.")
    else:
        lines.append(f"Your {metric} spiked this {window}.")
    if driver:
        lines.append(f"Looks driven by {driver.replace('_', ' ')}.")
    offer = _best_offer_for(merchant)
    if offer:
        lines.append(f"Good moment to push {offer.get('title')} while attention is up.")
    cta = f"{_want_me_to(hinglish)} draft a Google post to ride the momentum?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"perf_spike metric={metric} delta={delta}",
    }


def _h_milestone_reached(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    metric = (payload.get("metric") or "milestone").replace("_", " ")
    value_now = payload.get("value_now") or payload.get("value") or payload.get("count")
    target = payload.get("milestone_value")
    is_imminent = payload.get("is_imminent")
    hinglish = _is_hinglish(merchant)

    if is_imminent and target and value_now is not None:
        gap = target - value_now if isinstance(target, (int, float)) and isinstance(value_now, (int, float)) else None
        if gap:
            lines = [f"You're at {value_now} {metric} — {gap} away from {target}."]
        else:
            lines = [f"You're at {value_now} {metric}, closing in on {target}."]
        cta = f"{_want_me_to(hinglish)} draft an ask to your last few happy customers to help close the gap?"
        why = f"{metric} at {value_now}/{target}, imminent"
    elif value_now:
        lines = [f"You crossed {value_now} {metric}!"]
        peer = _peer_stats(category).get("avg_review_count")
        if peer and "review" in metric:
            lines.append(f"Category average is ~{peer}, so this puts you ahead of most peers.")
        cta = f"{_want_me_to(hinglish)} turn this into a shareable Google post?"
        why = f"{metric}={value_now}"
    else:
        return _h_generic(category, merchant, trigger, customer)

    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": why,
    }


def _h_dormant_with_vera(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    days = payload.get("days_since_last_contact") or payload.get("days")
    hinglish = _is_hinglish(merchant)
    lines = []
    if days:
        lines.append(f"Haven't heard from you in {days} days.")
    else:
        lines.append("Haven't heard from you in a while.")
    lines.append("No action needed from your side — just curious:")
    cta = "What's the one thing you'd want help with this week?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": "dormant_with_vera — curiosity/ask-the-merchant lever, no fabricated data",
    }


def _h_customer_lapsed(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    if not customer:
        return _h_generic(category, merchant, trigger, customer)
    payload = trigger.get("payload", {}) or {}
    hinglish = _is_hinglish(merchant, customer)
    cust_name = (customer.get("identity", {}) or {}).get("name", "there")
    merchant_name = (merchant.get("identity", {}) or {}).get("owner_first_name") or (merchant.get("identity", {}) or {}).get("name", "we")
    rel = customer.get("relationship", {}) or {}
    days = payload.get("days_since_last_visit")
    lines = [f"{merchant_name} here."]
    if days:
        lines.append(f"It's been about {days} days — happens to a lot of members, no judgment.")
    focus = payload.get("previous_focus")
    services = rel.get("services_received") or []
    if focus:
        lines.append(f"Following up since your focus was {focus.replace('_', ' ')}.")
    elif services:
        lines.append(f"Following up since your focus was around {services[-1].replace('_', ' ')}.")
    offer = _best_offer_for(merchant)
    if offer:
        lines.append(f"{offer.get('title')} — no commitment.")
    cta = "Reply YES to hold a spot, no charge unless you confirm."
    return {
        "lines": lines, "cta_type": "binary_yes_no", "cta_text": cta,
        "send_as": "merchant_on_behalf",
        "why": f"customer_lapsed for {cust_name}, days_since_last_visit={days}",
    }


def _h_festival_upcoming(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    festival = payload.get("festival")
    if not festival:
        return _h_generic(category, merchant, trigger, customer)
    days_until = payload.get("days_until")
    hinglish = _is_hinglish(merchant)
    lines = [f"{festival} is {days_until} days out." if days_until is not None else f"{festival} is coming up."]
    offer = _best_offer_for(merchant)
    if days_until is not None and days_until > 45:
        # Too early to push a promo — decision-quality: ask instead of pitch.
        lines.append("A bit early to push a promo, but worth planning now.")
        cta = f"{_want_me_to(hinglish)} sketch a {festival} offer idea for you to review closer to the date?"
    else:
        if offer:
            lines.append(f"You already have {offer.get('title')} live — good fit for a {festival} push.")
        cta = f"{_want_me_to(hinglish)} draft the {festival} post + WhatsApp broadcast?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"festival_upcoming {festival}, days_until={days_until}",
    }


def _h_category_seasonal(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    season = payload.get("season")
    trends = payload.get("trends") or []
    if not season and not trends:
        return _h_generic(category, merchant, trigger, customer)
    hinglish = _is_hinglish(merchant)
    lines = [f"{season.replace('_', ' ').title()} shift in demand." if season else "Seasonal demand shift."]
    if trends:
        # trends look like "ORS_demand_+40" — surface up to 2 as-is, they're
        # already specific and sourced from the trigger payload.
        readable = [t.replace("_", " ") for t in trends[:2]]
        lines.append(", ".join(readable) + ".")
    if payload.get("shelf_action_recommended"):
        cta = f"{_want_me_to(hinglish)} suggest a shelf/stock adjustment based on this?"
    else:
        cta = f"{_want_me_to(hinglish)} draft a post around the trending items?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"category_seasonal season={season} trends={trends[:2]}",
    }


def _h_competitor_opened(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    name = payload.get("competitor_name")
    distance = payload.get("distance_km") or payload.get("distance")
    their_offer = payload.get("their_offer")
    hinglish = _is_hinglish(merchant)
    if name:
        lines = [f"{name} just opened" + (f", about {distance}km away." if distance else " nearby.")]
    else:
        lines = ["A new competitor listed nearby on Google" + (f", about {distance}km away." if distance else ".")]
    if their_offer:
        lines.append(f"They're running {their_offer} — worth knowing what you're up against.")
    cta = f"{_want_me_to(hinglish)} check what's missing on your profile vs theirs?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"competitor_opened name={name} distance={distance}",
    }


def _h_review_theme_emerged(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    hinglish = _is_hinglish(merchant)

    # Trigger payload is the "why now" source of truth; merchant.review_themes
    # (if present) only corroborates it with a matching quote/count.
    theme = payload.get("theme")
    occurrences = payload.get("occurrences_30d")
    quote = payload.get("common_quote")
    trend = payload.get("trend")
    if not theme:
        negative = [t for t in merchant.get("review_themes", []) or [] if t.get("sentiment") == "neg"]
        if not negative:
            return _h_generic(category, merchant, trigger, customer)
        top = max(negative, key=lambda t: t.get("occurrences_30d", 0))
        theme, occurrences, quote = top.get("theme"), top.get("occurrences_30d"), top.get("common_quote")

    theme_label = (theme or "").replace("_", " ")
    lines = [f"{occurrences or 'Several'} reviews this month mention {theme_label}" +
             (f", trend {trend}." if trend else ".")]
    if quote:
        lines.append(f'One reads: "{quote}"')
    cta = f"{_want_me_to(hinglish)} draft a short reply template + one operational fix to try?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"review_theme '{theme}' x{occurrences}",
    }


def _h_supply_alert(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    hinglish = _is_hinglish(merchant)
    batches = payload.get("affected_batches") or payload.get("batches") or []
    manufacturer = payload.get("manufacturer")
    molecule = payload.get("molecule")
    subject = molecule or "a product"
    lines = [f"Urgent: voluntary recall on {subject}" +
             (f" (batches {', '.join(batches)})" if batches else "") +
             (f" by {manufacturer}." if manufacturer else ".")]
    count = payload.get("affected_customer_count")
    if count:
        lines.append(f"{count} of your customers were dispensed these in the last 90 days.")
    cta = f"{_want_me_to(hinglish)} draft their WhatsApp note + a replacement-pickup workflow?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"supply_alert molecule={molecule} batches={batches}",
    }


def _h_chronic_refill_due(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    if not customer:
        return _h_generic(category, merchant, trigger, customer)
    payload = trigger.get("payload", {}) or {}
    hinglish = _is_hinglish(merchant, customer)
    merchant_name = (merchant.get("identity", {}) or {}).get("name", "the pharmacy")
    medicines = payload.get("molecule_list") or payload.get("medicines") or []
    due_date = payload.get("stock_runs_out_iso", "")[:10] or payload.get("due_date") or payload.get("run_out_date")
    lines = [f"{merchant_name} here."]
    if medicines:
        lines.append(f"Your {len(medicines)} monthly medicines ({', '.join(medicines)}) run out around {due_date}." if due_date
                      else f"Your monthly medicines ({', '.join(medicines)}) are due for refill.")
    offer = _best_offer_for(merchant)
    if offer:
        lines.append(f"{offer.get('title')}.")
    cta = "Reply CONFIRM to dispatch, or call if anything's changed."
    return {
        "lines": lines, "cta_type": "binary_confirm_cancel", "cta_text": cta,
        "send_as": "merchant_on_behalf",
        "why": f"chronic_refill_due medicines={medicines} due={due_date}",
    }


def _h_renewal_due(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    days = payload.get("days_remaining")
    plan = payload.get("plan")
    amount = payload.get("renewal_amount")
    hinglish = _is_hinglish(merchant)
    lines = [f"Your {plan or 'subscription'} plan renews in {days} days." if days else "Your subscription renewal is coming up."]
    if amount:
        lines.append(f"Renewal amount: ₹{amount}.")
    cta = "Reply YES to auto-renew, or tell us if anything should change first."
    return {
        "lines": lines, "cta_type": "binary_yes_no", "cta_text": cta,
        "send_as": "vera",
        "why": f"renewal_due days_remaining={days}",
    }


def _h_curious_ask(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    name = (merchant.get("identity", {}) or {}).get("name", "there")
    hinglish = _is_hinglish(merchant)
    lines = ["Quick one — no dashboard needed."]
    cta = "What's been the most-asked-for service this week? I'll turn it into a post + a ready reply for pricing questions."
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": "scheduled curiosity-ask cadence — asking-the-merchant lever",
    }


def _h_gbp_unverified(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    hinglish = _is_hinglish(merchant)
    uplift = payload.get("estimated_uplift_pct")
    lines = ["Your Google Business Profile is still unverified — that caps your visibility in local search."]
    if uplift:
        lines.append(f"Verifying typically lifts visibility ~{_pct(uplift, 0)}.")
    path = payload.get("verification_path")
    cta = f"{_want_me_to(hinglish)} walk you through the {path.replace('_', ' ')} step?" if path else \
          f"{_want_me_to(hinglish)} walk you through verification?"
    return {
        "lines": lines, "cta_type": "binary_yes_no", "cta_text": cta,
        "send_as": "vera",
        "why": f"gbp_unverified uplift={uplift}",
    }


def _h_active_planning(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    topic = (payload.get("intent_topic") or payload.get("topic") or "your idea").replace("_", " ")
    last_msg = payload.get("merchant_last_message")
    hinglish = _is_hinglish(merchant)
    lines = [f"Picking up on {topic}."]
    if last_msg:
        lines.append(f'You said: "{last_msg}" — here’s a starter version, you can edit.')
    offer = _best_offer_for(merchant)
    if offer:
        lines.append(f"Structuring it around {offer.get('title')} to start.")
    cta = "Want the first draft now, or should I ask a couple more questions first?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"active_planning_intent topic={topic}",
    }


def _h_ipl_match(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    teams = payload.get("teams") or payload.get("match")
    time_ = payload.get("time")
    hinglish = _is_hinglish(merchant)
    lines = []
    if teams:
        lines.append(f"{teams} today" + (f", {time_}." if time_ else "."))
    else:
        lines.append("IPL match today.")
    offer = _best_offer_for(merchant)
    if offer:
        lines.append(f"You already have {offer.get('title')} live — good fit for a match-night push.")
    cta = f"{_want_me_to(hinglish)} draft a match-day delivery banner?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"ipl_match_today teams={teams}",
    }


def _h_wedding_or_trial_followup(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    if not customer:
        return _h_generic(category, merchant, trigger, customer)
    payload = trigger.get("payload", {}) or {}
    hinglish = _is_hinglish(merchant, customer)
    cust_name = (customer.get("identity", {}) or {}).get("name", "there")
    merchant_name = (merchant.get("identity", {}) or {}).get("owner_first_name") or (merchant.get("identity", {}) or {}).get("name", "we")
    event_date = payload.get("wedding_date") or payload.get("event_date")
    days_out = payload.get("days_to_wedding")
    lines = [f"{merchant_name} here."]
    if days_out is not None:
        lines.append(f"{days_out} days to your wedding — good window to lock in the next session.")
    elif event_date:
        lines.append(f"Good time to lock in the next session before things get busy closer to {event_date}.")
    else:
        lines.append("Following up on your last visit with us.")

    sessions = payload.get("next_session_options") or []
    slot_labels = [s.get("label") for s in sessions if s.get("label")]
    offer = _best_offer_for(merchant)
    if offer:
        lines.append(f"{offer.get('title')}.")
    if slot_labels:
        lines.append(f"Next slot: {slot_labels[0]}.")
        cta = "Want me to lock that slot in?"
    else:
        cta = "Want me to hold your preferred slot?"
    return {
        "lines": lines, "cta_type": "binary_yes_no", "cta_text": cta,
        "send_as": "merchant_on_behalf",
        "why": f"followup for {cust_name}, days_to_wedding={days_out}, event_date={event_date}",
    }


def _h_cde_opportunity(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    payload = trigger.get("payload", {}) or {}
    title = payload.get("title") or payload.get("webinar_title") or "a CDE session"
    date = payload.get("date")
    hinglish = _is_hinglish(merchant)
    lines = [f"New CDE opportunity: {title}" + (f", {date}." if date else ".")]
    cta = "Reply YES and I'll send the registration details."
    return {
        "lines": lines, "cta_type": "binary_yes_no", "cta_text": cta,
        "send_as": "vera",
        "why": f"cde_opportunity {title}",
    }


def _h_appointment_tomorrow(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    """The dataset generator emits only a {"placeholder": true, ...} payload for
    this kind on generated (non-seed) triggers — it has no real appointment
    date/time to ground on. Rather than fabricate one, this grounds on the real
    CustomerContext fields that *are* populated (relationship, preferences)."""
    if not customer:
        return _h_generic(category, merchant, trigger, customer)
    payload = trigger.get("payload", {}) or {}
    hinglish = _is_hinglish(merchant, customer)
    merchant_name = (merchant.get("identity", {}) or {}).get("name", "we")
    rel = customer.get("relationship", {}) or {}
    visits = rel.get("visits_total")

    lines = [f"{merchant_name} here.", "Quick reminder — your appointment is tomorrow."]
    if visits:
        lines.append(f"Will be visit #{visits + 1} with us.")
    slot_pref = (customer.get("preferences", {}) or {}).get("preferred_slots")
    if slot_pref:
        lines.append(f"Noted your usual {slot_pref.replace('_', ' ')} preference for next time.")
    # If a future dataset version populates real fields on this placeholder
    # payload, use them instead of only the generic reminder above.
    real_time = payload.get("time") or payload.get("slot_label")
    if real_time:
        lines[1] = f"Quick reminder — your appointment is tomorrow, {real_time}."

    cta = "Reply CONFIRM to keep it, or RESCHEDULE if you need a different time."
    return {
        "lines": lines, "cta_type": "binary_confirm_cancel", "cta_text": cta,
        "send_as": "merchant_on_behalf",
        "why": f"appointment_tomorrow, visits_total={visits}" + ("" if real_time else " (trigger payload had no real time — grounded on CustomerContext instead of fabricating one)"),
    }


def _h_winback_eligible(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    """merchant-scope: their own subscription/plan lapsed, not a customer lapse."""
    payload = trigger.get("payload", {}) or {}
    hinglish = _is_hinglish(merchant)
    days = payload.get("days_since_expiry")
    dip = payload.get("perf_dip_pct")
    added = payload.get("lapsed_customers_added_since_expiry")
    lines = []
    if days:
        lines.append(f"It's been {days} days since your plan lapsed.")
    if dip is not None:
        lines.append(f"Visibility is down {_signed_pct(dip)} since then.")
    if added:
        lines.append(f"{added} more customers went quiet in that window.")
    cta = f"{_want_me_to(hinglish)} show you the quickest way back to where you were?"
    return {
        "lines": lines or ["Your plan's been inactive for a bit."], "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera",
        "why": f"winback_eligible days_since_expiry={days} perf_dip_pct={dip}",
    }


def _h_generic(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> dict:
    """Fallback for any trigger kind without a dedicated handler — including
    kinds the judge injects post-submission that this bot has never seen.
    Grounds on whatever scalar facts are actually present in trigger.payload
    (never invents), plus merchant identity/signals for personalization."""
    payload = trigger.get("payload", {}) or {}
    kind_label = (trigger.get("kind") or "update").replace("_", " ")
    hinglish = _is_hinglish(merchant, customer)

    facts = []
    for k, v in payload.items():
        if k in ("category", "top_item_id", "merchant_id", "customer_id"):
            continue
        if isinstance(v, (int, float)):
            facts.append(f"{k.replace('_', ' ')}: {v}")
        elif isinstance(v, str) and 0 < len(v) < 60:
            facts.append(f"{k.replace('_', ' ')}: {v}")
        if len(facts) >= 2:
            break

    lines = [f"Heads up — {kind_label}."]
    if facts:
        lines.append(", ".join(facts) + ".")
    signals = merchant.get("signals", []) or []
    if signals:
        lines.append(f"Noting this against your current signal: {signals[0].replace(':', ' ').replace('_', ' ')}.")
    cta = f"{_want_me_to(hinglish)} look into this and suggest one next step?"
    return {
        "lines": lines, "cta_type": "open_ended", "cta_text": cta,
        "send_as": "vera" if not customer else "merchant_on_behalf",
        "why": f"unrecognized kind '{trigger.get('kind')}' — generic fact extraction from payload, no fabrication",
    }


_KIND_HANDLERS = {
    "research_digest": _h_research_digest,
    "category_research_digest_release": _h_research_digest,
    "regulation_change": _h_regulation_change,
    "recall_due": _h_recall_due,
    "perf_dip": _h_perf_dip,
    "seasonal_perf_dip": _h_perf_dip,
    "perf_spike": _h_perf_spike,
    "milestone_reached": _h_milestone_reached,
    "dormant_with_vera": _h_dormant_with_vera,
    "customer_lapsed_soft": _h_customer_lapsed,
    "customer_lapsed_hard": _h_customer_lapsed,
    "winback_eligible": _h_winback_eligible,
    "festival_upcoming": _h_festival_upcoming,
    "competitor_opened": _h_competitor_opened,
    "category_trend_movement": _h_generic,
    "category_seasonal": _h_category_seasonal,
    "review_theme_emerged": _h_review_theme_emerged,
    "supply_alert": _h_supply_alert,
    "chronic_refill_due": _h_chronic_refill_due,
    "renewal_due": _h_renewal_due,
    "curious_ask_due": _h_curious_ask,
    "scheduled_recurring": _h_curious_ask,
    "gbp_unverified": _h_gbp_unverified,
    "active_planning_intent": _h_active_planning,
    "ipl_match_today": _h_ipl_match,
    "wedding_package_followup": _h_wedding_or_trial_followup,
    "trial_followup": _h_wedding_or_trial_followup,
    "cde_opportunity": _h_cde_opportunity,
    "appointment_tomorrow": _h_appointment_tomorrow,
    "unplanned_slot_open": _h_recall_due,
}


# ---------------------------------------------------------------------------
# Public composition entry point
# ---------------------------------------------------------------------------

def compose_message(category: Ctx, merchant: Ctx, trigger: Ctx,
                     customer: Optional[Ctx] = None,
                     prior_bodies: Optional[list[str]] = None) -> dict[str, Any]:
    """Returns dict with keys: body, cta, send_as, suppression_key, rationale,
    template_name, template_params — matching challenge-brief.md §5 / §7."""
    if COMPOSER_MODE == "llm" and LLM_API_KEY:
        try:
            return _llm_compose(category, merchant, trigger, customer, prior_bodies or [])
        except Exception as exc:  # noqa: BLE001 - never let an LLM hiccup empty a tick
            return _template_compose(category, merchant, trigger, customer, prior_bodies or [],
                                      llm_fallback_reason=str(exc))
    return _template_compose(category, merchant, trigger, customer, prior_bodies or [])


def _template_compose(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx],
                       prior_bodies: list[str], llm_fallback_reason: Optional[str] = None) -> dict[str, Any]:
    kind = trigger.get("kind", "")
    handler = _KIND_HANDLERS.get(kind, _h_generic)
    result = handler(category, merchant, trigger, customer)

    salutation = _salutation(category, merchant) if not customer else (customer.get("identity", {}) or {}).get("name", "there")
    emoji = " 🦷" if category.get("slug") == "dentists" and customer else ""
    body_parts = [f"{salutation},{emoji}"] + result["lines"] + [result["cta_text"]]
    body = " ".join(p for p in body_parts if p).strip()
    body = _strip_urls(body)
    body = _dedupe_against(body, prior_bodies, result)

    rationale = f"[{trigger.get('kind')}] {result['why']}; cta={result['cta_type']}; send_as={result['send_as']}"
    if llm_fallback_reason:
        rationale += f"; llm_unavailable_used_template ({llm_fallback_reason[:80]})"

    return {
        "body": body,
        "cta": result["cta_type"],
        "send_as": result["send_as"],
        "suppression_key": trigger.get("suppression_key") or trigger.get("id", ""),
        "rationale": rationale,
        "template_name": f"{'merchant' if result['send_as'] == 'merchant_on_behalf' else 'vera'}_{trigger.get('kind', 'generic')}_v1",
        "template_params": [salutation] + result["lines"][:2],
    }


def _dedupe_against(body: str, prior_bodies: list[str], result: dict) -> str:
    """Anti-repetition: if this body is near-identical to something already
    sent in this conversation, deterministically vary the CTA phrasing rather
    than resend verbatim (api-call-examples.md §10 anti-repetition penalty)."""
    for prev in prior_bodies:
        if body == prev or SequenceMatcher(None, body, prev).ratio() > 0.92:
            return body + " (following up on this)"
    return body


# ---------------------------------------------------------------------------
# LLM path (optional; disabled unless COMPOSER_MODE=llm and LLM_API_KEY set)
# ---------------------------------------------------------------------------

_LLM_SYSTEM = """You are the composer for a WhatsApp merchant-growth assistant (like magicpin's "Vera").
Given CategoryContext, MerchantContext, TriggerContext, and an optional CustomerContext, produce ONE
message. Rules:
- Anchor on a concrete, verifiable fact already present in the given JSON (number, date, name, offer).
  NEVER invent a fact, citation, competitor name, or offer that isn't in the input.
- Match category voice/vocab; avoid taboo words listed in voice.vocab_taboo.
- Personalize to the specific merchant (their name/owner, their numbers, their offers).
- Exactly one CTA. No URLs. No multi-choice CTA unless this is a booking/slot flow.
- Use Hindi-English code-mix when merchant/customer language preference indicates it.
- Keep it concise; no preambles ("I hope you're doing well...").
Respond ONLY with strict JSON: {"body": str, "cta": "open_ended"|"binary_yes_no"|"binary_confirm_cancel"|"multi_choice_slot"|"none", "send_as": "vera"|"merchant_on_behalf", "rationale": str}"""


def _llm_compose(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx],
                  prior_bodies: list[str]) -> dict[str, Any]:
    user_prompt = json.dumps({
        "category": category, "merchant": merchant, "trigger": trigger,
        "customer": customer, "already_sent_in_this_conversation": prior_bodies,
    }, ensure_ascii=False)

    if LLM_PROVIDER == "anthropic":
        req_body = json.dumps({
            "model": LLM_MODEL, "max_tokens": 600, "temperature": 0,
            "system": _LLM_SYSTEM,
            "messages": [{"role": "user", "content": user_prompt}],
        }).encode("utf-8")
        req = urlrequest.Request(
            "https://api.anthropic.com/v1/messages", data=req_body,
            headers={"x-api-key": LLM_API_KEY, "Content-Type": "application/json",
                     "anthropic-version": "2023-06-01"},
        )
        resp = urlrequest.urlopen(req, timeout=LLM_TIMEOUT)
        data = json.loads(resp.read().decode("utf-8"))
        raw = data["content"][0]["text"]
    else:
        raise RuntimeError(f"LLM_PROVIDER '{LLM_PROVIDER}' not wired in this scaffold — use 'anthropic' or COMPOSER_MODE=template")

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError("LLM did not return JSON")
    parsed = json.loads(match.group())
    body = _strip_urls(str(parsed.get("body", "")).strip())
    if not body:
        raise ValueError("LLM returned empty body")
    body = _dedupe_against(body, prior_bodies, {})

    return {
        "body": body,
        "cta": parsed.get("cta", "open_ended"),
        "send_as": parsed.get("send_as", "merchant_on_behalf" if customer else "vera"),
        "suppression_key": trigger.get("suppression_key") or trigger.get("id", ""),
        "rationale": parsed.get("rationale", "llm-composed"),
        "template_name": f"llm_{trigger.get('kind', 'generic')}_v1",
        "template_params": [body[:120]],
    }


# ---------------------------------------------------------------------------
# Trigger selection ("decision quality"): pick the single best trigger per
# merchant (or per merchant+customer) out of everything available this tick.
# ---------------------------------------------------------------------------

def score_trigger(trigger: Ctx, merchant: Optional[Ctx], category: Optional[Ctx]) -> float:
    score = float(trigger.get("urgency", 1)) * 10
    kind = trigger.get("kind", "")
    signals = " ".join((merchant or {}).get("signals", []) or [])
    if "perf" in kind and ("ctr_below_peer" in signals or "perf" in signals):
        score += 5
    if kind in ("recall_due", "chronic_refill_due", "customer_lapsed_hard", "customer_lapsed_soft"):
        score += 3  # customer-relationship triggers are high-intent, low-abuse-risk
    if kind == "regulation_change":
        score += 2  # compliance is rarely spammy, worth prioritizing
    return score


def rank_triggers(triggers: list[Ctx], merchant: Optional[Ctx], category: Optional[Ctx]) -> list[Ctx]:
    return sorted(triggers, key=lambda t: (-score_trigger(t, merchant, category), t.get("expires_at") or ""))
