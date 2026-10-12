#!/usr/bin/env python3
"""Synthetic review exercise v1. No network, accounts, models or credentials.

Run: python review-anchor.py
Checks structured evidence cards, NOT natural-language answer quality.
The accompanying review-anchor.md defines the separate blind-reader exercise.
"""
import copy
import hashlib
import json

SOURCE = {
    "revision": "synthetic-config-v1",
    "path": "/example/config.json",
    "text": "configured_peer=old-peer\nexpected_peer=new-peer\nrestart_required=unknown\n",
}
SOURCE_HASH = hashlib.sha256(SOURCE["text"].encode()).hexdigest()


def review(source, card):
    """Require current evidence before assessing an actionable finding."""
    if source is None:
        return "UNGRADEABLE", ["source_unavailable"]
    if card.get("revision") != source["revision"]:
        return "UNGRADEABLE", ["revision_mismatch"]
    digest = hashlib.sha256(source["text"].encode()).hexdigest()
    if card.get("source_sha256") != digest:
        return "UNGRADEABLE", ["source_hash_mismatch"]
    quotes = card.get("quotes")
    if not isinstance(quotes, list) or not quotes:
        return "INCOMPLETE", ["missing_observation"]
    if any(not isinstance(q, str) or not q or q not in source["text"].splitlines() for q in quotes):
        return "REJECT", ["unsupported_observation"]
    # This rubric is specific to this frozen synthetic task, not a general judge.
    issues = []
    if card.get("location") != source["path"]:
        issues.append("missing_location")
    if not {"configured_peer=old-peer", "expected_peer=new-peer"}.issubset(quotes):
        issues.append("missing_comparison")
    if card.get("finding") != "peer_mismatch":
        issues.append("missing_diagnosis")
    if card.get("next_step") != "confirm_expected_peer_before_edit":
        issues.append("unsupported_or_missing_next_step")
    if card.get("restart_required") != "unknown" or "restart_required=unknown" not in quotes:
        issues.append("unsupported_restart_claim")
    return ("INCOMPLETE", issues) if issues else ("ACCEPT", [])


def run():
    good = dict(revision=SOURCE["revision"], source_sha256=SOURCE_HASH,
                location=SOURCE["path"], quotes=SOURCE["text"].splitlines(),
                finding="peer_mismatch", next_step="confirm_expected_peer_before_edit",
                restart_required="unknown")
    cases = []
    def case(name, expected, changes=None, source=SOURCE):
        card = copy.deepcopy(good)
        card.update(changes or {})
        cases.append((name, expected, source, card))
    case("grounded_specific", "ACCEPT")
    case("alternate_wording", "ACCEPT", {"explanation": "The configured and expected peers differ."})
    case("bland_but_current", "INCOMPLETE", {"location": None, "finding": "possible_config_issue", "next_step": "investigate"})
    case("invented_detail", "REJECT", {"quotes": ["configured_peer=third-peer"]})
    case("unsupported_restart", "INCOMPLETE", {"restart_required": "yes"})
    case("stale_but_readable", "UNGRADEABLE", {"revision": "synthetic-config-v0"})
    case("unreachable", "UNGRADEABLE", source=None)
    case("changed_source", "UNGRADEABLE", source={**SOURCE, "text": SOURCE["text"] + "changed=true\n"})
    case("missing_observation", "INCOMPLETE", {"quotes": []})
    results = []
    for name, expected, source, card in cases:
        verdict, reasons = review(source, card)
        if verdict != expected:
            raise AssertionError(f"{name}: expected {expected}, got {verdict}")
        results.append(dict(case=name, expected=expected, verdict=verdict, reasons=reasons))
    print(json.dumps(dict(fixture_version=1, source_sha256=SOURCE_HASH,
                         cases=len(results), passed=len(results), results=results), indent=2))


if __name__ == "__main__":
    run()
