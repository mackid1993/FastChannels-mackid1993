#!/usr/bin/env python3
"""Cautious, adversarial review of a cached overlay fix before it is merged and released.

A held PR's fix already applies cleanly and passes the static checks against the current
upstream — this is the *second* set of eyes that the deterministic checks can't give: does
the fix actually do the right thing for THIS upstream, or did upstream shift something that
makes it wrong/incomplete? Reading and judging an existing fix costs far fewer tokens than
regenerating one, and it's a skeptical double-check before anything ships.

Usage:  git diff <upstream-sha> HEAD | review_fix.py <path-to-AGENTS.md>
Env:    AI_MODEL (default anthropic/claude-haiku-5-5) and its key: ANTHROPIC_API_KEY for an
        anthropic/ model, OPENROUTER_API_KEY for an openrouter/ one.
Prints: 'APPROVE' on the first line if safe to merge+release, else 'CONCERNS: <reason>'.
        Fail-safe: any error or missing key prints CONCERNS, so the caller never blind-reuses.
"""
import json
import os
import sys
import urllib.request

SYSTEM = (
    "You are a cautious, adversarial reviewer. A CACHED fix for the FastChannels DirecTV "
    "overlay (a patch of its own modules plus one-line hooks into upstream files) is about to "
    "be MERGED and RELEASED. It ALREADY applies cleanly and passes the static checks against "
    "the current upstream, so do not re-check that. Your job is to be skeptical and decide "
    "whether it is actually CORRECT and COMPLETE for THIS upstream. Look for a real reason it "
    "is wrong: a hook wired into a plausible-but-wrong place, upstream changing the meaning of "
    "a name a hook uses, a backported re-implementation reading the wrong real source or "
    "returning the wrong shape, a dropped or no-op'd hook, or invented/fake data. AGENTS.md "
    "(the overlay's intent and full hook table) and the fix's diff are provided. Think it "
    "through, then END your reply with a FINAL line that is exactly 'APPROVE' (nothing else) if "
    "it is correct and safe to merge and release, or 'CONCERNS: <one specific reason>'. If you "
    "are unsure or lack the context to be confident, end with CONCERNS — be cautious about what "
    "gets released."
)


def _call_anthropic(model: str, key: str, user: str) -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": 8192,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    resp = json.load(urllib.request.urlopen(req, timeout=180))
    return "".join(b.get("text", "") for b in resp.get("content") or [] if b.get("type") == "text")


def _call_openrouter(model: str, key: str, user: str) -> str:
    # Provider routing shared with the AI repair (openrouter_provider.py): never OpenInference.
    from openrouter_provider import provider
    body = json.dumps({
        "model": model,
        "provider": provider(),
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    resp = json.load(urllib.request.urlopen(req, timeout=180))
    return resp["choices"][0]["message"]["content"] or ""


def main() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from openrouter_provider import is_anthropic, key_var
    model = os.environ.get("AI_MODEL", "anthropic/claude-haiku-5-5")
    var = key_var(model)
    key = os.environ.get(var, "")
    if not key:
        print("CONCERNS: no " + var + " for the review")
        return
    diff = sys.stdin.read()
    if len(diff) > 120000:
        # Fail-closed: if the change is too big to show the reviewer in full, don't let a
        # partial view approve it — make it a concern so the caller regenerates instead.
        print("CONCERNS: change too large to review in full (" + str(len(diff)) + " chars)")
        return
    agents = ""
    if len(sys.argv) > 1:
        try:
            with open(sys.argv[1], encoding="utf-8") as f:
                agents = f.read()[:40000]
        except OSError:
            pass
    user = (
        "=== AGENTS.md (overlay intent + hook table) ===\n" + agents
        + "\n\n=== FIX DIFF (the cached fix about to be merged + released) ===\n" + diff
    )
    # Generous output budget: reasoning models spend tokens thinking before the verdict, and
    # a truncated verdict reads as CONCERNS. No temperature (some models reject it).
    try:
        if is_anthropic(model):
            content = _call_anthropic(model[len("anthropic/"):], key, user)
        else:
            content = _call_openrouter(model[len("openrouter/"):] if model.startswith("openrouter/") else model,
                                       key, user)
        content = content.strip()
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        verdict = lines[-1] if lines else ""
        # Fail-closed: APPROVE only when the final line is unambiguously APPROVE AND nothing in
        # the reply raised a concern. A reasoning model may object and then end on a hedged line
        # like "Approve only if that's intentional" — that must NOT read as approval.
        final = verdict.upper().rstrip(" .!,:;-")
        raised_concern = any("CONCERN" in ln.upper() for ln in lines)
        if final == "APPROVE" and not raised_concern:
            print("APPROVE")
        else:
            concern = next((ln for ln in lines if "CONCERN" in ln.upper()), "")
            print(concern or ("CONCERNS: ambiguous verdict (" + (verdict[:100] or "empty response") + ")"))
    except Exception as exc:  # noqa: BLE001 — any failure must fail safe to CONCERNS
        print("CONCERNS: review call failed (" + str(exc)[:120] + ")")


if __name__ == "__main__":
    main()
