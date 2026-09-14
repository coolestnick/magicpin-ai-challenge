"""
In-memory state for the magicpin AI Challenge bot.

Two stores:
- ContextStore        — versioned, idempotent (scope, context_id) -> payload
- ConversationStore    — per-conversation turn state + suppression-key -> conversation
                         lookup, used to decide whether a trigger has already been
                         acted on and what the conversation's current mode is.

Both are plain in-memory dicts. The testing brief only requires the bot to persist
state for the lifetime of one test run (no restarts mid-test), so nothing fancier
(Redis/SQLite) is needed for the challenge itself — swap in a real store for
production use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ContextEntry:
    version: int
    payload: dict[str, Any]


class ContextStore:
    """Versioned store. Higher `version` for the same (scope, context_id) replaces
    the prior value atomically; a version <= the stored one is a no-op (per
    challenge-testing-brief.md §2.1)."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], ContextEntry] = {}

    def put(self, scope: str, context_id: str, version: int, payload: dict[str, Any]) -> tuple[bool, int]:
        key = (scope, context_id)
        cur = self._data.get(key)
        if cur is not None and cur.version >= version:
            return False, cur.version
        self._data[key] = ContextEntry(version=version, payload=payload)
        return True, version

    def get(self, scope: str, context_id: str) -> Optional[dict[str, Any]]:
        entry = self._data.get((scope, context_id))
        return entry.payload if entry else None

    def all_ids(self, scope: str) -> list[str]:
        return [cid for (s, cid) in self._data if s == scope]

    def all_payloads(self, scope: str) -> dict[str, dict[str, Any]]:
        return {cid: e.payload for (s, cid), e in self._data.items() if s == scope}

    def counts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for scope, _cid in self._data:
            counts[scope] = counts.get(scope, 0) + 1
        return counts

    def wipe(self) -> None:
        self._data.clear()


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: Optional[str]
    customer_id: Optional[str] = None
    trigger_id: Optional[str] = None
    suppression_key: Optional[str] = None
    status: str = "active"          # active | waiting | ended
    mode: str = "pitch"             # pitch | action  (flips on explicit merchant commitment)
    turn: int = 1
    sent_bodies: list[str] = field(default_factory=list)
    merchant_messages: list[str] = field(default_factory=list)
    auto_reply_streak: int = 0
    started_at: float = field(default_factory=time.time)


class ConversationStore:
    """Tracks conversations and merchant-level opt-outs.

    Suppression is *state*-based, not wall-clock-based: the judge advances
    simulated time in 5-minute ticks that don't line up with real elapsed
    time, so "don't resend for N seconds" is the wrong primitive here.
    Instead: a suppression_key that already has an *active* (not yet replied
    to / not ended) conversation is skipped; one whose conversation is
    "waiting" is fair game again next tick; "ended" is permanent for that key.
    """

    def __init__(self) -> None:
        self._conversations: dict[str, ConversationState] = {}
        self._by_suppression_key: dict[str, str] = {}
        self._suppressed_merchants: set[str] = set()   # hard opt-outs / hostility

    def get(self, conversation_id: str) -> Optional[ConversationState]:
        return self._conversations.get(conversation_id)

    def status_for_key(self, suppression_key: str) -> Optional[str]:
        cid = self._by_suppression_key.get(suppression_key)
        if not cid:
            return None
        cs = self._conversations.get(cid)
        return cs.status if cs else None

    def create(self, conversation_id: str, **kwargs: Any) -> ConversationState:
        cs = ConversationState(conversation_id=conversation_id, **kwargs)
        self._conversations[conversation_id] = cs
        if cs.suppression_key:
            self._by_suppression_key[cs.suppression_key] = conversation_id
        return cs

    def get_or_create_for_reply(self, conversation_id: str, merchant_id: Optional[str],
                                  customer_id: Optional[str]) -> ConversationState:
        """/v1/reply can legally arrive for a conversation_id the bot never
        created via /v1/tick (e.g. replay-test scenarios call /v1/reply
        directly). Handle that gracefully instead of erroring."""
        cs = self._conversations.get(conversation_id)
        if cs is None:
            cs = self.create(conversation_id, merchant_id=merchant_id, customer_id=customer_id)
        return cs

    def is_merchant_suppressed(self, merchant_id: Optional[str]) -> bool:
        return bool(merchant_id) and merchant_id in self._suppressed_merchants

    def suppress_merchant(self, merchant_id: Optional[str]) -> None:
        if merchant_id:
            self._suppressed_merchants.add(merchant_id)

    def wipe(self) -> None:
        self._conversations.clear()
        self._by_suppression_key.clear()
        self._suppressed_merchants.clear()
