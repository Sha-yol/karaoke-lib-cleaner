#!/usr/bin/env python3
"""Run llm_parse batch files through the Anthropic Batch API — no agentic loop.

`llm_parse extract` emits batch-NNN.json; `llm_parse ingest` consumes results-NNN.json. This
tool is the middle: it hands each batch file to the model ONCE, as a single stateless request,
and writes the answer back in the exact shape `ingest` already validates.

WHY THIS EXISTS. The same work was previously done by handing batch files to subagents. That
costs a multiple of the token volume the task actually needs — a harness system prompt, tool
schemas, file reads, and a multi-turn transcript, per batch, none of which the model needs in
order to segment a filename. Here the billable payload is exactly the system prompt plus the
batch JSON, once, at the Batch API's 50% discount.

    submit   -> POST every un-answered batch in a run as one Batch API job
    poll     -> processing_status + per-request counts
    collect  -> write results-NNN.json for each succeeded request

Then, unchanged, per result file:

    python3 tools/llm_parse.py ingest --results <...>/results-NNN.json --model claude-sonnet-5

WHAT IS DELIBERATELY NOT CONFIGURABLE. The system prompt, the user prompt and the batch files
come from `llm_parse` itself, imported rather than re-implemented. A second copy of the prompt
that drifts from the first is exactly the defect PROMPT_VERSION exists to make visible, and it
would be invisible here: the batch files carry `prompt_version` but nothing would check that
the prompt used to answer them was that version. Importing is the check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.llm_parse import PROMPT_VERSION, SYSTEM_PROMPT, run_dir, user_prompt  # noqa: E402

MODEL = "claude-sonnet-5"

# Measured on the 23 sweep-v2 batches already answered: 92.1 output tokens/item on average,
# 107.4 at the worst. 200 items therefore lands near 18.5k and has never exceeded ~21.5k.
# 32k is ~1.5x the observed ceiling — a truncated answer costs a whole batch turnaround, and
# unused output tokens cost nothing at all, so the asymmetry says round up.
MAX_TOKENS = 32000

# Sonnet 5's minimum cacheable prefix is 1024 tokens; the system prompt measures 1,203, so the
# breakpoint does engage rather than silently no-op. It is worth ~1 cent across a 13-batch run
# (see --estimate) because the system prompt is ~1.3% of the payload — the batch JSON below it
# is unique per request and uncacheable by construction. Kept because it is free and correct,
# not because it is load-bearing.
SYSTEM_BLOCK = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

STATE_NAME = "batch-api.json"
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _client():
    """Accepts ANTHROPIC_API_KEY or ANTHROPIC_KEY — this project's shell exports the latter."""
    try:
        import anthropic
    except ImportError:
        raise SystemExit("the `anthropic` package is not importable — pip install anthropic, "
                         "or set PYTHONPATH to a directory it was installed into")
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_KEY")
    if not key:
        raise SystemExit("no API key: set ANTHROPIC_API_KEY (or ANTHROPIC_KEY)")
    return anthropic.Anthropic(api_key=key)


def _nnn(p: Path) -> str:
    return p.stem.split("-")[-1]


def pending(dest: Path) -> list[Path]:
    """Batch files with no results file beside them. This is the whole resume story: a run is
    re-submittable at any time and will only ever re-ask what was never answered."""
    return [b for b in sorted(dest.glob("batch-*.json"))
            if not (dest / f"results-{_nnn(b)}.json").exists()]


def _requests(batches: list[Path], thinking: bool, effort: str | None) -> list[dict]:
    reqs = []
    for b in batches:
        batch = json.loads(b.read_text(encoding="utf-8"))
        got = batch.get("prompt_version")
        if got != PROMPT_VERSION:
            # The answers land in llm_parses stamped PROMPT_VERSION. Sending a batch built by a
            # different prompt version would stamp them with a version that did not produce
            # them, and §6.2 scoring groups by exactly that column.
            raise SystemExit(f"{b.name}: prompt_version {got!r} != {PROMPT_VERSION!r} — this "
                             f"batch was built by a different prompt. Re-extract it.")
        params = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_BLOCK,
            "messages": [{"role": "user", "content": user_prompt(batch)}],
        }
        # Off by default. The 92 tok/item output profile this run is costed against was measured
        # on answers produced without it, and rule 6 already gives the model a `confidence`
        # channel for the uncertainty thinking would otherwise resolve. --thinking is here for a
        # deliberate quality experiment, not as a default to drift into.
        params["thinking"] = {"type": "adaptive"} if thinking else {"type": "disabled"}
        if effort:
            params["output_config"] = {"effort": effort}
        reqs.append({"custom_id": b.stem, "params": params})
    return reqs


def cmd_submit(args) -> int:
    dest = run_dir(args.run)
    if not dest.is_dir():
        raise SystemExit(f"no such run directory: {dest}")
    state_path = dest / STATE_NAME
    if state_path.exists() and not args.force:
        st = json.loads(state_path.read_text(encoding="utf-8"))
        raise SystemExit(f"{state_path} already records batch {st.get('id')} — `poll` it, or "
                         f"`collect` it, or pass --force to submit a second job")
    todo = pending(dest)
    if not todo:
        print(f"nothing to do: every batch in {dest} already has a results file")
        return 0

    reqs = _requests(todo, args.thinking, args.effort)
    print(f"run       {dest}")
    print(f"batches   {len(todo)}  ({', '.join(_nnn(b) for b in todo)})")
    print(f"model     {MODEL}   thinking={'adaptive' if args.thinking else 'disabled'}"
          + (f"  effort={args.effort}" if args.effort else ""))
    if args.dry_run:
        print("DRY RUN — nothing submitted")
        return 0

    batch = _client().messages.batches.create(requests=reqs)
    state_path.write_text(json.dumps(
        {"id": batch.id, "model": MODEL, "prompt_version": PROMPT_VERSION,
         "created_at": str(batch.created_at), "custom_ids": [b.stem for b in todo],
         "thinking": bool(args.thinking), "effort": args.effort},
        indent=1), encoding="utf-8")
    print(f"\nsubmitted {batch.id}  status={batch.processing_status}")
    print(f"state     {state_path}")
    print(f"\n  poll:    python3 tools/llm_batch.py poll --run {args.run}")
    return 0


def _state(dest: Path) -> dict:
    p = dest / STATE_NAME
    if not p.exists():
        raise SystemExit(f"no {STATE_NAME} in {dest} — submit first")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_poll(args) -> int:
    dest = run_dir(args.run)
    st = _state(dest)
    b = _client().messages.batches.retrieve(st["id"])
    c = b.request_counts
    print(f"{b.id}  {b.processing_status}")
    print(f"  processing {c.processing}   succeeded {c.succeeded}   errored {c.errored}   "
          f"canceled {c.canceled}   expired {c.expired}")
    if b.processing_status == "ended":
        print(f"\n  collect: python3 tools/llm_batch.py collect --run {args.run}")
    return 0


def _extract(text: str) -> object:
    """The prompt forbids a markdown fence; a fence is still the one deviation worth tolerating,
    because the alternative is discarding 200 correct answers over three backticks. Anything
    else is left to fail loudly in `ingest`, which requeues per-stem."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    return json.loads(_FENCE_RE.sub("", text.strip()))


def cmd_collect(args) -> int:
    dest = run_dir(args.run)
    st = _state(dest)
    client = _client()
    b = client.messages.batches.retrieve(st["id"])
    if b.processing_status != "ended" and not args.force:
        raise SystemExit(f"batch {b.id} is {b.processing_status}, not ended — "
                         f"poll until it ends, or --force to collect what is ready")

    wrote, failed, usage = [], [], {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    for r in client.messages.batches.results(st["id"]):
        cid, kind = r.custom_id, r.result.type
        if kind != "succeeded":
            failed.append(f"{cid}: {kind}")
            continue
        msg = r.result.message
        u = msg.usage
        usage["in"] += u.input_tokens
        usage["out"] += u.output_tokens
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        if msg.stop_reason == "max_tokens":
            # Truncated JSON is not partially salvageable, and writing it would hand `ingest` a
            # file that fails wholesale. Leave no results file: `submit` re-asks this batch.
            failed.append(f"{cid}: stop_reason=max_tokens (raise MAX_TOKENS and re-submit)")
            continue
        text = "".join(blk.text for blk in msg.content if blk.type == "text")
        try:
            payload = _extract(text)
        except ValueError as e:
            failed.append(f"{cid}: response is not JSON ({e})")
            continue
        n = _nnn(Path(cid + ".json"))
        out = dest / f"results-{n}.json"
        tmp = out.with_suffix(".json.part")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(out)
        wrote.append(out)

    print(f"--- COLLECT {st['id']} ---")
    for p in wrote:
        n = len(json.loads(p.read_text(encoding='utf-8')).get("results", []))
        print(f"  wrote {p.name}  ({n} results)")
    for f in failed:
        print(f"  FAILED {f}")
    print(f"\n  usage: in {usage['in']:,}  out {usage['out']:,}  "
          f"cache_read {usage['cache_read']:,}  cache_write {usage['cache_write']:,}")
    print(f"  cost:  ${_cost(usage):.4f}  (batch rates, {MODEL})")
    if wrote:
        print(f"\n  ingest them:\n    for f in {dest}/results-*.json; do \\\n"
              f"      python3 tools/llm_parse.py ingest --results \"$f\" --model {MODEL}; done")
    if failed:
        print(f"\n  {len(failed)} request(s) produced no results file — re-run `submit --force` "
              f"to ask again for exactly those.")
    return 0


# Batch API rates for claude-sonnet-5 (50% of the $2/$10 standard), verified against
# platform.claude.com/docs/en/about-claude/pricing on 2026-08-14. The $2/$10 introductory
# price is now the permanent standard price — the scheduled 2026-09-01 rise to $3/$15 was
# cancelled — so this table has no expiry to track.
BATCH_IN, BATCH_OUT = 1.00, 5.00          # $/MTok
BATCH_CACHE_READ, BATCH_CACHE_WRITE = 0.10, 1.25


def _cost(u: dict) -> float:
    return (u["in"] * BATCH_IN + u["out"] * BATCH_OUT
            + u["cache_read"] * BATCH_CACHE_READ + u["cache_write"] * BATCH_CACHE_WRITE) / 1e6


def cmd_estimate(args) -> int:
    """Priced off count_tokens for the input (exact) and the run's own answered batches for the
    output (measured tok/item), so neither number is a guess about how Hebrew tokenizes."""
    dest = run_dir(args.run)
    todo = pending(dest)
    if not todo:
        print("nothing pending")
        return 0
    client = _client()

    tin, items = 0, 0
    for b in todo:
        batch = json.loads(b.read_text(encoding="utf-8"))
        tin += client.messages.count_tokens(
            model=MODEL, system=SYSTEM_BLOCK,
            messages=[{"role": "user", "content": user_prompt(batch)}]).input_tokens
        items += len(batch["items"])

    per_item = []
    for p in sorted(dest.glob("results-*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        n = len(r.get("results", []))
        if n:
            t = client.messages.count_tokens(
                model=MODEL,
                messages=[{"role": "user", "content": json.dumps(r, ensure_ascii=False)}])
            per_item.append(t.input_tokens / n)
    if not per_item:
        raise SystemExit("no answered batches in this run to measure an output rate from")

    lo, mid, hi = min(per_item), sum(per_item) / len(per_item), max(per_item)
    print(f"run        {dest}")
    print(f"pending    {len(todo)} batches / {items:,} stems")
    print(f"input      {tin:,} tokens (count_tokens, exact)")
    print(f"output     {items * mid:,.0f} tokens projected "
          f"({items * lo:,.0f}-{items * hi:,.0f}; {mid:.1f} tok/item measured over "
          f"{len(per_item)} answered batches)")
    for label, o in (("low", items * lo), ("expected", items * mid), ("high", items * hi)):
        c = _cost({"in": tin, "out": o, "cache_read": 0, "cache_write": 0})
        print(f"  {label:9} ${c:.2f}")
    print(f"\n  (batch rates ${BATCH_IN}/${BATCH_OUT} per MTok; synchronous would be "
          f"${_cost({'in': tin, 'out': items * mid, 'cache_read': 0, 'cache_write': 0}) * 2:.2f})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (("submit", cmd_submit, "post un-answered batches as one job"),
                               ("poll", cmd_poll, "check processing status"),
                               ("collect", cmd_collect, "write results-NNN.json"),
                               ("estimate", cmd_estimate, "priced dry run, submits nothing")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--run", required=True, help="run name under artifacts/llm-parse/")
        p.set_defaults(func=fn)
        if name == "submit":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--force", action="store_true",
                           help="submit even though a batch-api.json already exists")
            p.add_argument("--thinking", action="store_true",
                           help="adaptive thinking (off by default — see _requests)")
            p.add_argument("--effort", default=None,
                           choices=["low", "medium", "high", "xhigh", "max"])
        if name == "collect":
            p.add_argument("--force", action="store_true",
                           help="collect from a job that has not ended")
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
