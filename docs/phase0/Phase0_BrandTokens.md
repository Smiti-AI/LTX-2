# Phase 0.4 — Brand Token Tokenizer Probe

**Text encoder:** Gemma 3 12B IT — `google/gemma-3-12b-it-qat-q4_0-unquantized`
**Tokenizer backend:** `sentencepiece-local` (SentencePiece, vocab ~262144)

## Source code touchpoints (LTX-2 trainer)

- Tokenizer wrapper: [packages/ltx-core/src/ltx_core/text_encoders/gemma/tokenizer.py:11](../../packages/ltx-core/src/ltx_core/text_encoders/gemma/tokenizer.py)
- Encoder call site (data prep): [packages/ltx-trainer/scripts/process_captions.py:317](../../packages/ltx-trainer/scripts/process_captions.py)
- Encoder call site (training validation): [packages/ltx-trainer/src/ltx_trainer/trainer.py:402](../../packages/ltx-trainer/src/ltx_trainer/trainer.py)
- **No `add_tokens` / `resize_token_embeddings` calls anywhere in the trainer.** The tokenizer is frozen.

## Candidate table

| Candidate | Format | # tokens | Single drawer? | Token IDs | SentencePiece pieces |
|---|---|---:|:---:|---|---|
| `CHASE_PP` | uppercase + suffix | 4 | ✗ | `2293,13554,236779,14616` | `CH · ASE · _ · PP` |
| `SKYE_PP` | uppercase + suffix | 4 | ✗ | `21775,66943,236779,14616` | `SK · YE · _ · PP` |
| `<chase_pp>` | angle-bracket pseudo-special | 6 | ✗ | `236820,574,781,236779,612,236813` | `< · ch · ase · _ · pp · >` |
| `<skye_pp>` | angle-bracket pseudo-special | 6 | ✗ | `236820,2592,3177,236779,612,236813` | `< · sk · ye · _ · pp · >` |
| `[CHASE]` | bracket pseudo-special | 4 | ✗ | `236840,2293,13554,236842` | `[ · CH · ASE · ]` |
| `[SKYE]` | bracket pseudo-special | 4 | ✗ | `236840,21775,66943,236842` | `[ · SK · YE · ]` |
| `chasepawpatrol` | concat-no-space | 5 | ✗ | `574,781,93218,3164,1427` | `ch · ase · paw · pat · rol` |
| `skyepawpatrol` | concat-no-space | 5 | ✗ | `16012,838,1348,3164,1427` | `sky · ep · aw · pat · rol` |
| `sks chase` | Dreambooth-style sks prefix | 3 | ✗ | `236751,2357,39839` | `s · ks · ▁chase` |
| `sks skye` | Dreambooth-style sks prefix | 4 | ✗ | `236751,2357,1566,3177` | `s · ks · ▁sk · ye` |
| `Chase` | vanilla character name | 1 | ✓ | `169522` | `Chase` |
| `Skye` | vanilla character name | 2 | ✗ | `14841,3177` | `Sk · ye` |
| `a paw patrol dog` | generic-concept baseline | 4 | ✗ | `236746,54381,42942,4799` | `a · ▁paw · ▁patrol · ▁dog` |

## Interpretation

A "single drawer" candidate (1 token) gives the model a clean dedicated embedding slot — but only if that drawer isn't already shared with general concepts. Multi-token candidates *re-use* embeddings of more general words, which is exactly what causes catastrophic forgetting (vision.md §Brand Alignment Philosophy).

**Surprise from the table:** `Chase` is a single Gemma token (ID 169522) — Gemma's vocabulary already has it as a frequent surname/verb. `Skye` is two subwords. So "single drawer" is not the right metric here: **`Chase` has its own drawer, but the drawer is already full of generic semantic baggage.** We want the *opposite* of single-token-baggage: a multi-subword pattern that's uncommon enough that the LoRA can carve out its own associations without overwriting Gemma's prior knowledge of unrelated words.

## Two strategic options

### Option A — multi-subword identifier (no tokenizer change)
Pick a candidate that consistently tokenizes as a *deterministic, distinctive* subword pattern that's unlikely to collide with anything in the general corpus. The LoRA learns to associate that subword sequence with the character. Compatible with the existing trainer code (no changes).

### Option B — true new tokens (tokenizer + embedding modification)
Add `<chase_pp>` / `<skye_pp>` (or similar) as **special tokens**. Requires:

- `tokenizer.add_tokens([...], special_tokens=True)` before training
- `text_encoder.resize_token_embeddings(len(tokenizer))` to extend the embedding matrix
- Initialize new embeddings (mean-of-vocab or copy-from-similar-token)
- Persist the modified tokenizer in every checkpoint (so inference matches training)

**The current LTX-Video-Trainer does NOT support this** — there are no calls to `add_tokens` / `resize_token_embeddings` anywhere in the codebase. Adopting Option B is a separate engineering task: trainer modification, plus careful checkpoint/eval handling. Per `active_plan.md §3.4`, this should be flagged for explicit approval before pursuing.

## Precedents from adjacent video/image LoRA work

- **Dreambooth (HuggingFace diffusers)** — uses `sks` as a 4-token rare prefix without modifying the tokenizer. Relies on subword overlap being uncommon. Works for narrow concepts but is known to suffer at scale ([diffusers/examples/dreambooth/train_dreambooth.py](https://github.com/huggingface/diffusers/blob/main/examples/dreambooth/train_dreambooth.py)).
- **Textual Inversion** — adds a *true* new token with a learnable embedding while freezing the rest of the encoder. Solves the "single drawer" problem cleanly but requires tokenizer modification ([diffusers/examples/textual_inversion/textual_inversion.py](https://github.com/huggingface/diffusers/blob/main/examples/textual_inversion/textual_inversion.py)).
- **AnimateDiff/Motion-LoRA** community LoRAs — almost all use Dreambooth-style multi-subword identifiers (e.g. `mtn_lora_zoom`, `xyz_character`) and rely on the LoRA itself learning the association. No tokenizer changes.

## Recommendation

**Go with Option A (multi-subword) using `CHASE_PP` and `SKYE_PP`.**

**Tokenizer modification required: NO.**

**Reasoning:**

1. Compatible with the existing `LTX-Video-Trainer` — zero core code changes. Honors the active_plan §3.4 constraint.
2. **The shared `_PP` suffix is a structural feature, not just a naming convention.** Both candidates end in `_` + `PP` (token IDs 236779 and 14616). The LoRA effectively gets a *brand-identifier suffix drawer* shared across all PP characters: `CHASE_PP`, `SKYE_PP`, and (later) `MARSHALL_PP`, `RUBBLE_PP` etc. all share the same final two tokens. The character is encoded in the prefix subwords; the brand is encoded in the suffix. This is a cleaner factorization than any single-token approach Gemma's vocab can offer us.
3. **Avoid `Chase` even though it's a single token** (id 169522). That drawer is already occupied by Gemma's generic notion of "chase" — verbs, proper nouns, last names. Training on it risks teaching the LoRA to *override* Gemma's general knowledge of "Chase" with our character, which is the catastrophic-forgetting failure mode `vision.md` warns about.
4. **Avoid `<chase_pp>` and `[CHASE]`** — they tokenize to *more* subwords (6 and 4 respectively) and the bracket characters add no signal. They look special-token-shaped but Gemma treats them as plain text, so we get the cost without the benefit.
5. Reversibility: if Option A produces poor brand alignment after the visual baseline runs, Option B (textual inversion / true new tokens with `add_tokens` + `resize_token_embeddings`) becomes a focused experiment with clear precedent — but only after we have evidence Option A is insufficient.

## Reproducibility

Re-run `python scripts/phase0/tokenizer_probe.py`. The output is byte-identical across runs on the same machine: SentencePiece tokenization is deterministic; the candidate list is fixed in source. The token IDs above will match exactly.
