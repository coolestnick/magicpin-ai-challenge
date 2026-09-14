# Live-site notes — magicpin.com/vera/ai-challenge

**Status**: Supplementary to `challenge-brief.md` / `challenge-testing-brief.md`.
**Captured**: 2026-09-15, from `partners.magicpin.com/vera/ai-challenge` (the
actual content-serving host — `magicpin.com/vera/ai-challenge` itself is an
empty Next.js shell that forwards there; fetching the shell directly returns
only a `<title>` tag and no body content).

Everything below is either **new** information not present in the local
`.md` briefs, or a **discrepancy** between the public page and the local
briefs worth knowing before building/submitting.

---

## 1. This is a hiring funnel, not a hackathon with prizes

Headline copy: *"India's Biggest AI Challenge — No Resume | No Interview | No
Experience Required."* Confirmed in the FAQ:

> **What this can lead to** — A full-time offer, or an internship that can
> convert into a full-time offer. Strong submissions help us find people who
> can join the team.
>
> **Who can apply?** — Solo applications only. Students and working
> professionals are both welcome. The full-time role is based out of our
> **Gurgaon office**. Before the final offer, we'll verify that you actually
> did the work yourself.

This fills in the placeholders `challenge-brief.md §14` deliberately left
blank (eligibility, prizes, team size — all answered above; solo only, not
pairs, contradicting the brief's "solo or pairs" placeholder guess).

## 2. Rubric naming: "Decision quality" replaces "Trigger relevance"

`challenge-brief.md §8` lists 5 dimensions: Specificity, Category fit,
Merchant fit, **Trigger relevance**, Engagement compulsion.

The live site — and, authoritatively, the `ScoreResult` dataclass and the
`LLMScorer.SYSTEM` prompt inside the bundled `judge_simulator.py` itself —
use **Decision quality** instead:

> *"Can your bot pick the best signal for this moment? Great outputs combine
> trigger + merchant state + category fit before writing."*

This is a broader ask than "did you cite the trigger": the bot should be able
to **rank/select** among multiple competing signals (several active triggers,
merchant state, category context) for a given merchant, not just react to
whichever single trigger it's handed. `bot/composer.py`'s `rank_triggers()`
implements this explicitly — it's the reason a tick with 4 available triggers
for one merchant produces exactly 1 action, not 4.

(`judge_simulator.py`'s parser is backward-compatible — it reads
`decision_quality` first, falling back to `trigger_relevance` if that key is
what a judge response uses — so this is very likely a rename/reframe of the
same dimension, not a 6th one added on top of 50.)

## 3. A new example trigger kind, not in the local dataset

The site's "Message craft" section shows this generic-vs-strong pair:

- ❌ *"Hi Doctor, want to run a discount campaign today to increase sales?"*
- ✅ *"190 people in your locality are searching for 'Dental Check Up'.
  Should I send them a discounted check up at ₹299?"*

The strong example implies a trigger kind like `local_search_demand` /
unmet-search-volume — **this kind does not appear anywhere in
`dataset/triggers_seed.json`'s 24 kinds** (confirmed by enumerating the seed
file directly). Treat it as illustrative of the *shape* judges want, not a
kind to special-case — `composer.py`'s generic fallback handler is what would
actually receive a kind like this if the real harness injects it
post-submission, since decision-quality scoring explicitly tests handling of
*unseen* trigger kinds gracefully.

## 4. The core warning, in the site's own words

> *"The local `judge_simulator` gives you a deterministic dry-run on the 30
> canonical test pairs. The actual judge harness uses the same scoring logic
> but injects new facts you haven't seen... Bots that pattern-match the
> simulator will fail. Bots that ground every output in the context they've
> actually been given will not."*

Also explicit: **what gets rejected quickly** — hallucinated facts, generic
templates, unstable/non-deterministic responses, broken endpoint behavior.

## 5. Logistics confirmed (filling `challenge-brief.md §14`)

| Item | Answer |
|---|---|
| Eligibility | Solo only; students + working professionals; no prior AI experience required |
| Team size | 1 (not pairs) |
| Submission window | Open now, no stated deadline — "submit anytime" |
| Resubmission | Allowed multiple times; only quality counts, not volume |
| Deliverable | One public bot URL (+ optional 1-page README) |
| Submission form fields | Full name, email, phone number, submission URL, LinkedIn (optional) |
| Prize / outcome | Full-time offer or convertible internship, Gurgaon-based |
| Selection outcome | "Selected candidates hear from the team after evaluation" |

## 6. Everything else matches exactly

5-context framework naming, all 5 endpoints, the warmup → test-window →
adaptive-injection → replay → score-report lifecycle, the 30s / 10rps / 500KB
/ 20-actions-per-tick limits, the zip package contents and file tree, and all
10 case studies — all identical between the live site and the local briefs.
The local files remain the authoritative, complete spec; this document only
captures what the site adds or reframes on top of them.
