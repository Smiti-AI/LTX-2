#!/usr/bin/env python3
"""Phase 0.4 — Brand Token Tokenizer Probe (active_plan §3.4).

LTX-2 uses Gemma 3 12B IT as its text encoder. The Gemma SentencePiece
tokenizer determines whether a brand identifier gets a clean dedicated
representation or shatters into subwords overlapping with generic concepts.
This script tokenizes each candidate format and prints the result.

Output: docs/phase0/Phase0_BrandTokens.md (Markdown report, idempotent).

Tokenizer load order:
  1. Local SentencePiece file at models/gemma-3-12b-it-qat-q4_0-unquantized/tokenizer.model
  2. Hugging Face hub: google/gemma-3-12b-it (fallback; uses ~/.cache/huggingface/token)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LOCAL_TOKENIZER_MODEL = REPO / "models" / "gemma-3-12b-it-qat-q4_0-unquantized" / "tokenizer.model"
OUT_REPORT = REPO / "docs" / "phase0" / "Phase0_BrandTokens.md"


CANDIDATES = [
    ("CHASE_PP", "uppercase + suffix"),
    ("SKYE_PP", "uppercase + suffix"),
    ("<chase_pp>", "angle-bracket pseudo-special"),
    ("<skye_pp>", "angle-bracket pseudo-special"),
    ("[CHASE]", "bracket pseudo-special"),
    ("[SKYE]", "bracket pseudo-special"),
    ("chasepawpatrol", "concat-no-space"),
    ("skyepawpatrol", "concat-no-space"),
    ("sks chase", "Dreambooth-style sks prefix"),
    ("sks skye", "Dreambooth-style sks prefix"),
    ("Chase", "vanilla character name"),
    ("Skye", "vanilla character name"),
    ("a paw patrol dog", "generic-concept baseline"),
]


@dataclass
class Probe:
    candidate: str
    note: str
    token_ids: list[int]
    pieces: list[str]

    @property
    def is_single(self) -> bool:
        return len(self.token_ids) == 1


def load_tokenizer():
    import sentencepiece as spm

    if LOCAL_TOKENIZER_MODEL.exists():
        sp = spm.SentencePieceProcessor()
        sp.load(str(LOCAL_TOKENIZER_MODEL))
        return ("sentencepiece-local", sp)

    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained("google/gemma-3-12b-it")
    return ("transformers-hub:google/gemma-3-12b-it", tok)


def tokenize_one(backend: str, tok, s: str) -> tuple[list[int], list[str]]:
    if backend.startswith("sentencepiece"):
        ids = tok.encode_as_ids(s)
        pieces = tok.encode_as_pieces(s)
        return ids, pieces
    enc = tok(s, add_special_tokens=False, return_tensors=None)
    ids = list(enc["input_ids"])
    pieces = tok.convert_ids_to_tokens(ids)
    return ids, pieces


def render_report(backend: str, probes: list[Probe]) -> str:
    lines: list[str] = []
    lines.append("# Phase 0.4 — Brand Token Tokenizer Probe")
    lines.append("")
    lines.append("**Text encoder:** Gemma 3 12B IT — `google/gemma-3-12b-it-qat-q4_0-unquantized`")
    lines.append(f"**Tokenizer backend:** `{backend}` (SentencePiece, vocab ~262144)")
    lines.append("")
    lines.append("## Source code touchpoints (LTX-2 trainer)")
    lines.append("")
    lines.append("- Tokenizer wrapper: [packages/ltx-core/src/ltx_core/text_encoders/gemma/tokenizer.py:11](../../packages/ltx-core/src/ltx_core/text_encoders/gemma/tokenizer.py)")
    lines.append("- Encoder call site (data prep): [packages/ltx-trainer/scripts/process_captions.py:317](../../packages/ltx-trainer/scripts/process_captions.py)")
    lines.append("- Encoder call site (training validation): [packages/ltx-trainer/src/ltx_trainer/trainer.py:402](../../packages/ltx-trainer/src/ltx_trainer/trainer.py)")
    lines.append("- **No `add_tokens` / `resize_token_embeddings` calls anywhere in the trainer.** The tokenizer is frozen.")
    lines.append("")
    lines.append("## Candidate table")
    lines.append("")
    lines.append("| Candidate | Format | # tokens | Single drawer? | Token IDs | SentencePiece pieces |")
    lines.append("|---|---|---:|:---:|---|---|")
    for p in probes:
        ids = ",".join(str(i) for i in p.token_ids)
        pieces = " · ".join(repr(x).strip("'") for x in p.pieces)
        single = "✓" if p.is_single else "✗"
        lines.append(f"| `{p.candidate}` | {p.note} | {len(p.token_ids)} | {single} | `{ids}` | `{pieces}` |")
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append(
        "A \"single drawer\" candidate (1 token) gives the model a clean dedicated "
        "embedding slot — but only if that drawer isn't already shared with general concepts. "
        "Multi-token candidates *re-use* embeddings of more general words, which is exactly "
        "what causes catastrophic forgetting (vision.md §Brand Alignment Philosophy)."
    )
    lines.append("")
    lines.append(
        "**Surprise from the table:** `Chase` is a single Gemma token (ID 169522) — "
        "Gemma's vocabulary already has it as a frequent surname/verb. "
        "`Skye` is two subwords. So \"single drawer\" is not the right metric here: "
        "**`Chase` has its own drawer, but the drawer is already full of generic semantic baggage.** "
        "We want the *opposite* of single-token-baggage: a multi-subword pattern that's "
        "uncommon enough that the LoRA can carve out its own associations without overwriting "
        "Gemma's prior knowledge of unrelated words."
    )
    lines.append("")
    lines.append("## Two strategic options")
    lines.append("")
    lines.append("### Option A — multi-subword identifier (no tokenizer change)")
    lines.append(
        "Pick a candidate that consistently tokenizes as a *deterministic, distinctive* "
        "subword pattern that's unlikely to collide with anything in the general corpus. "
        "The LoRA learns to associate that subword sequence with the character. "
        "Compatible with the existing trainer code (no changes)."
    )
    lines.append("")
    lines.append("### Option B — true new tokens (tokenizer + embedding modification)")
    lines.append(
        "Add `<chase_pp>` / `<skye_pp>` (or similar) as **special tokens**. Requires:"
    )
    lines.append("")
    lines.append("- `tokenizer.add_tokens([...], special_tokens=True)` before training")
    lines.append("- `text_encoder.resize_token_embeddings(len(tokenizer))` to extend the embedding matrix")
    lines.append("- Initialize new embeddings (mean-of-vocab or copy-from-similar-token)")
    lines.append("- Persist the modified tokenizer in every checkpoint (so inference matches training)")
    lines.append("")
    lines.append(
        "**The current LTX-Video-Trainer does NOT support this** — there are no calls to "
        "`add_tokens` / `resize_token_embeddings` anywhere in the codebase. Adopting Option B is "
        "a separate engineering task: trainer modification, plus careful checkpoint/eval handling. "
        "Per `active_plan.md §3.4`, this should be flagged for explicit approval before pursuing."
    )
    lines.append("")
    lines.append("## Precedents from adjacent video/image LoRA work")
    lines.append("")
    lines.append(
        "- **Dreambooth (HuggingFace diffusers)** — uses `sks` as a 4-token rare prefix without modifying the tokenizer. "
        "Relies on subword overlap being uncommon. Works for narrow concepts but is known to suffer at scale "
        "([diffusers/examples/dreambooth/train_dreambooth.py](https://github.com/huggingface/diffusers/blob/main/examples/dreambooth/train_dreambooth.py))."
    )
    lines.append(
        "- **Textual Inversion** — adds a *true* new token with a learnable embedding while freezing the rest of the encoder. "
        "Solves the \"single drawer\" problem cleanly but requires tokenizer modification "
        "([diffusers/examples/textual_inversion/textual_inversion.py](https://github.com/huggingface/diffusers/blob/main/examples/textual_inversion/textual_inversion.py))."
    )
    lines.append(
        "- **AnimateDiff/Motion-LoRA** community LoRAs — almost all use Dreambooth-style multi-subword identifiers "
        "(e.g. `mtn_lora_zoom`, `xyz_character`) and rely on the LoRA itself learning the association. "
        "No tokenizer changes."
    )
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append("**Go with Option A (multi-subword) using `CHASE_PP` and `SKYE_PP`.**")
    lines.append("")
    lines.append("**Tokenizer modification required: NO.**")
    lines.append("")
    lines.append("**Reasoning:**")
    lines.append("")
    lines.append(
        "1. Compatible with the existing `LTX-Video-Trainer` — zero core code changes. Honors the "
        "active_plan §3.4 constraint."
    )
    lines.append(
        "2. **The shared `_PP` suffix is a structural feature, not just a naming convention.** "
        "Both candidates end in `_` + `PP` (token IDs 236779 and 14616). The LoRA effectively gets "
        "a *brand-identifier suffix drawer* shared across all PP characters: `CHASE_PP`, `SKYE_PP`, "
        "and (later) `MARSHALL_PP`, `RUBBLE_PP` etc. all share the same final two tokens. "
        "The character is encoded in the prefix subwords; the brand is encoded in the suffix. "
        "This is a cleaner factorization than any single-token approach Gemma's vocab can offer us."
    )
    lines.append(
        "3. **Avoid `Chase` even though it's a single token** (id 169522). That drawer is already "
        "occupied by Gemma's generic notion of \"chase\" — verbs, proper nouns, last names. "
        "Training on it risks teaching the LoRA to *override* Gemma's general knowledge of \"Chase\" "
        "with our character, which is the catastrophic-forgetting failure mode `vision.md` warns about."
    )
    lines.append(
        "4. **Avoid `<chase_pp>` and `[CHASE]`** — they tokenize to *more* subwords (6 and 4 "
        "respectively) and the bracket characters add no signal. They look special-token-shaped "
        "but Gemma treats them as plain text, so we get the cost without the benefit."
    )
    lines.append(
        "5. Reversibility: if Option A produces poor brand alignment after the visual baseline runs, "
        "Option B (textual inversion / true new tokens with `add_tokens` + `resize_token_embeddings`) "
        "becomes a focused experiment with clear precedent — but only after we have evidence Option A "
        "is insufficient."
    )
    lines.append("")
    lines.append("## Reproducibility")
    lines.append("")
    lines.append(
        "Re-run `python scripts/phase0/tokenizer_probe.py`. The output is byte-identical across runs "
        "on the same machine: SentencePiece tokenization is deterministic; the candidate list is "
        "fixed in source. The token IDs above will match exactly."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    backend, tok = load_tokenizer()
    probes: list[Probe] = []
    for cand, note in CANDIDATES:
        ids, pieces = tokenize_one(backend, tok, cand)
        probes.append(Probe(candidate=cand, note=note, token_ids=ids, pieces=pieces))
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text(render_report(backend, probes))
    print(f"backend: {backend}", flush=True)
    for p in probes:
        flag = "single" if p.is_single else f"{len(p.token_ids)} subw"
        print(f"  {p.candidate:25s}  [{flag:8s}]  {p.token_ids}  ->  {p.pieces}", flush=True)
    print(f"\nwrote {OUT_REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
