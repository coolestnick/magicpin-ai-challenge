"""
magicpin AI Challenge — bot HTTP server.

Implements the 5 endpoints from challenge-testing-brief.md §2:
  POST /v1/context   GET /v1/healthz
  POST /v1/tick       GET /v1/metadata
  POST /v1/reply

Run:
    pip install -r requirements.txt
    uvicorn bot:app --host 0.0.0.0 --port 8080

Self-test against the bundled judge simulator:
    export BOT_URL=http://localhost:8080
    python ../judge_simulator.py
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import composer
import reply_router
from store import ContextStore, ConversationStore

app = FastAPI(title="magicpin AI Challenge — Vera-Better Bot")

START = time.time()
VERSION = "0.1.0"

ctx_store = ContextStore()
conv_store = ConversationStore()

TEAM_NAME = os.environ.get("TEAM_NAME", "unnamed-team")
TEAM_MEMBERS = [m.strip() for m in os.environ.get("TEAM_MEMBERS", "").split(",") if m.strip()]
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
SUBMITTED_AT = os.environ.get("SUBMITTED_AT", "")

MAX_ACTIONS_PER_TICK = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Malformed-request handling -> spec's 400 {accepted: false, reason: invalid_scope}
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if request.url.path == "/v1/context":
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "invalid_scope", "details": str(exc.errors())[:500],
        })
    return JSONResponse(status_code=400, content={"error": "malformed_request", "details": str(exc.errors())[:500]})


# ---------------------------------------------------------------------------
# GET /v1/healthz, GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": ctx_store.counts(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": composer.model_name(),
        "approach": (
            "Deterministic 4-context composer: per-trigger-kind templates grounded strictly in "
            "pushed context fields (no fabrication), with an optional temperature=0 LLM upgrade path. "
            "Decision-quality trigger ranking picks the single best signal per merchant per tick. "
            "Rule-based reply router handles auto-reply detection, opt-out/hostility, explicit intent "
            "transitions, and off-topic redirects."
        ),
        "contact_email": CONTACT_EMAIL,
        "version": VERSION,
        "submitted_at": SUBMITTED_AT,
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class ContextPush(BaseModel):
    scope: Literal["category", "merchant", "customer", "trigger"]
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: ContextPush):
    accepted, current_version = ctx_store.put(body.scope, body.context_id, body.version, body.payload)
    if not accepted:
        return JSONResponse(status_code=409, content={
            "accepted": False, "reason": "stale_version", "current_version": current_version,
        })
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions: list[dict[str, Any]] = []

    triggers_by_merchant_lane: dict[tuple[str, Optional[str]], list[dict]] = {}
    for trg_id in body.available_triggers:
        trg = ctx_store.get("trigger", trg_id)
        if not trg:
            continue
        merchant_id = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        if not merchant_id:
            continue
        if conv_store.is_merchant_suppressed(merchant_id):
            continue
        suppression_key = trg.get("suppression_key") or trg.get("id", "")
        status = conv_store.status_for_key(suppression_key)
        if status == "active" or status == "ended":
            continue  # already sent and awaiting reply, or opted out / done
        expires_at = trg.get("expires_at")
        if expires_at and expires_at < body.now:
            continue
        customer_id = trg.get("customer_id") if trg.get("scope") == "customer" else None
        lane = (merchant_id, customer_id)
        triggers_by_merchant_lane.setdefault(lane, []).append(trg)

    for (merchant_id, customer_id), candidates in triggers_by_merchant_lane.items():
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break
        merchant = ctx_store.get("merchant", merchant_id)
        if not merchant:
            continue
        category = ctx_store.get("category", merchant.get("category_slug", ""))
        if not category:
            continue
        customer = ctx_store.get("customer", customer_id) if customer_id else None

        best = composer.rank_triggers(candidates, merchant, category)[0]
        composed = composer.compose_message(category, merchant, best, customer, prior_bodies=[])
        if not composed.get("body"):
            continue

        conversation_id = f"conv_{merchant_id}_{best.get('id', 'trg')}"
        conv_store.create(
            conversation_id, merchant_id=merchant_id, customer_id=customer_id,
            trigger_id=best.get("id"), suppression_key=composed["suppression_key"],
            status="active",
        )
        cs = conv_store.get(conversation_id)
        cs.sent_bodies.append(composed["body"])

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": best.get("id"),
            "template_name": composed["template_name"],
            "template_params": composed["template_params"],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    return {"actions": actions[:MAX_ACTIONS_PER_TICK]}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: int = 0


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    return reply_router.handle_reply(ctx_store, conv_store, body.model_dump())


# ---------------------------------------------------------------------------
# Optional teardown (challenge-testing-brief.md §11)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    ctx_store.wipe()
    conv_store.wipe()
    return {"status": "wiped"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
