#!/usr/bin/env python3
"""
Produces submission.jsonl (challenge-brief.md §7.2) by calling the composer
directly against the 30 canonical test pairs — no running server needed.

Usage:
    python generate_submission.py --expanded-dir ../expanded --out ../submission.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import composer


def load_expanded(expanded_dir: Path) -> dict:
    categories = {}
    for f in (expanded_dir / "categories").glob("*.json"):
        c = json.load(open(f, encoding="utf-8"))
        categories[c["slug"]] = c

    def load_dir(name: str, key: str) -> dict:
        out = {}
        for f in (expanded_dir / name).glob("*.json"):
            item = json.load(open(f, encoding="utf-8"))
            out[item[key]] = item
        return out

    merchants = load_dir("merchants", "merchant_id")
    customers = load_dir("customers", "customer_id")
    triggers = load_dir("triggers", "id")

    test_pairs = json.load(open(expanded_dir / "test_pairs.json", encoding="utf-8"))
    pairs = test_pairs.get("pairs", test_pairs if isinstance(test_pairs, list) else [])
    return {"categories": categories, "merchants": merchants, "customers": customers,
            "triggers": triggers, "pairs": pairs}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expanded-dir", default="../expanded")
    ap.add_argument("--out", default="../submission.jsonl")
    args = ap.parse_args()

    data = load_expanded(Path(args.expanded_dir))
    lines = []
    for pair in data["pairs"]:
        test_id = pair["test_id"]
        trigger = data["triggers"].get(pair["trigger_id"])
        merchant = data["merchants"].get(pair["merchant_id"])
        customer = data["customers"].get(pair["customer_id"]) if pair.get("customer_id") else None
        if not trigger or not merchant:
            print(f"  [skip] {test_id}: missing trigger or merchant in expanded dataset")
            continue
        category = data["categories"].get(merchant.get("category_slug", ""))
        if not category:
            print(f"  [skip] {test_id}: missing category '{merchant.get('category_slug')}'")
            continue

        composed = composer.compose_message(category, merchant, trigger, customer, prior_bodies=[])
        lines.append(json.dumps({
            "test_id": test_id,
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        }, ensure_ascii=False))

    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(lines)} lines to {args.out}")


if __name__ == "__main__":
    main()
