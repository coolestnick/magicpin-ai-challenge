# Bot README

## Approach

A **deterministic, template-based composer** (default) with an optional **LLM
upgrade path** (`COMPOSER_MODE=llm`, temperature=0), sharing one codebase:

- **`store.py`** — versioned `ContextStore` (idempotent by `(scope, context_id,
  version)`) and `ConversationStore` (per-conversation turn state, plus
  suppression tracked by *conversation status* rather than wall-clock time,
  since the judge's simulated ticks don't map to real elapsed seconds).
- **`composer.py`** — the composition brain:
  - `rank_triggers()` implements the **decision-quality** step: groups
    available triggers into "lanes" (one per merchant, or per merchant+customer
    for customer-scoped triggers) and picks the single best one per lane per
    tick, scored by urgency + signal alignment + relationship-trigger priority.
  - One hand-written handler per trigger `kind` (24 kinds — the full set found
    in `dataset/triggers_seed.json`), each grounded strictly in fields actually
    present in the pushed context. A generic fallback handles any kind without
    a dedicated handler (including kinds the judge injects post-submission
    that this bot has never seen) by extracting whatever scalar facts exist in
    `trigger.payload` — never inventing one.
  - Shared helpers for language detection (hi-en code-mix), owner/salutation
    resolution, offer lookup, digest lookup, peer-stat comparison, and
    anti-repetition (near-duplicate detection against bodies already sent in
    the conversation).
- **`reply_router.py`** — the `/v1/reply` state machine, in priority order:
  auto-reply detection (canned-phrase regex + verbatim-repeat detection; 1st
  occurrence → nudge, 2nd → `wait`, 3rd+ → `end`) → hard opt-out / hostility →
  `end` → explicit intent commitment → `send` in action mode (no more
  qualifying questions) → off-topic redirect → engaged on-topic follow-up.
- **`bot.py`** — wires the above into the 5 required endpoints, plus the
  spec's exact status codes (`409` stale-version, `400` invalid-scope).

## Tradeoffs

- **Template mode is the default**, not the LLM path. It's zero-dependency,
  instantly deterministic, and every message it produces is traceable to a
  specific field in the input — which made it much faster to catch fabrication
  bugs during development (e.g. a wrong payload key name shows up immediately
  as a missing fact, not a plausible-sounding LLM guess). The LLM path exists
  and is wired (`COMPOSER_MODE=llm`), but wasn't the default because "never
  fabricate" is easier to *guarantee* structurally than to prompt for.
- **One trigger per merchant-lane per tick** is a deliberate restraint choice
  (challenge-testing-brief.md FAQ: "restraint is rewarded, spam is
  penalized") — even when 3-4 triggers are available for the same merchant in
  one tick, only the highest-scored one is sent.
- **Offer selection is "first active offer"**, not keyword-matched to the
  trigger's topic (e.g. a bridal-followup trigger may pull a generic offer
  rather than a bridal-specific one if the merchant has several active). A
  keyword-matched picker (`_best_offer_for(merchant, keyword=...)` already
  exists but isn't wired into every handler) would be the first quality
  improvement with more time.
- **Generated (non-seed) triggers in the expanded dataset carry placeholder
  payloads** for kinds without a seed exemplar — `{"placeholder": true,
  "metric_or_topic": "<kind>"}` — e.g. most `appointment_tomorrow` and
  `customer_lapsed_soft` instances beyond the original seeds. The composer
  degrades gracefully here: it grounds on whatever real `CustomerContext` /
  `MerchantContext` fields exist (visit count, preferences, offers) rather
  than inventing a time or number. A handful of `submission.jsonl` lines are
  visibly thinner because of this — that's the dataset, not a fabricated fact.

## What additional context would have helped most

1. **A real offer→trigger relevance mapping** in `CategoryContext.offer_catalog`
   (e.g. tagging `den_003 "Teeth Whitening"` as relevant to `wedding` /
   `bridal` triggers) so offer selection could be topic-aware without
   guessing from title-string keywords.
2. **Populated payloads for every generated trigger**, not just the 25 seeds —
   the placeholder-payload gap above is the single biggest cap on specificity
   for ~40% of the expanded (non-seed) trigger instances.
3. **A canonical trigger-kind → payload-schema reference** (a JSON Schema per
   `kind`) would have removed most of the guesswork here — several handlers
   were built on a wrong assumed field name until checked against the actual
   seed JSON (e.g. `supply_alert.affected_batches`, not `.batches`).

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # optional — defaults work with no env vars at all
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Self-test against the bundled judge simulator (from the repo root):

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py   # needs an LLM_API_KEY set inside the file for scoring
```

Regenerate the full dataset + the submission file:

```bash
python dataset/generate_dataset.py --seed-dir dataset --out expanded
python bot/generate_submission.py --expanded-dir expanded --out submission.jsonl
```
