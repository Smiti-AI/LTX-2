# ADR-001 — Brand-token format for character LoRAs

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-04-29 |
| **Deciders** | project lead, Phase 0 brand-token research |
| **Supersedes** | (first ADR; supersedes the implicit `CHASE`/`SKYE` uppercase-bare convention used in pre-rebuild trainer configs `chase_exp1_highcap.yaml`, `goldenPLUS150_*.yaml`) |
| **Related** | [`docs/phase0/Phase0_BrandTokens.md`](../phase0/Phase0_BrandTokens.md), [`vision.md` § Brand Alignment Philosophy](../vision.md), `scripts/dataset_pipeline/brand_tokens.yaml` |

---

## Context

`vision.md` makes catastrophic forgetting and "token bleeding" the central risk of fine-tuning LTX-2 on Paw Patrol characters: the base model has strong priors for "chase" (the verb), "Skye" (the human name / cockpit pilot), "German Shepherd", "Cockapoo". Training a LoRA against captions that contain those generic words risks **diluting** the brand into the model's existing concept distribution rather than carving out a fresh representational drawer for the *specific* Paw Patrol character.

The Phase 0 task `§3.4 Brand Token Format — Empirical Research` evaluated candidate token formats against three criteria:
1. **Tokenizes cleanly** through Gemma — produces a stable, reproducible subword sequence.
2. **No collisions** with the verb "chase", the name "Skye", or breed descriptors that already exist in Gemma's vocabulary.
3. **Implementable without forking the LTX-Video-Trainer** (a hard project constraint at the time of decision).

Six candidate forms were probed (`Phase0_BrandTokens.md`):
- `CHASE_PP` / `SKYE_PP` (multi-subword w/ underscore separator)
- `<chase_pp>` / `<skye_pp>` (XML-style; relies on tokenizer treating `<…>` specially — it doesn't)
- `[CHASE]` / `[SKYE]` (bracket-wrapped; same issue as above)
- `chasepawpatrol` / `skyepawpatrol` (no separator; tokenizes inconsistently)
- `sks chase` / `sks skye` (DreamBooth convention; collides with the verb "chase")
- True new tokens added via `tokenizer.add_tokens()` + embedding-matrix resize ("Option B")

---

## Decision

**Adopt `CHASE_PP` and `SKYE_PP` as the brand-token format.** Add new characters by registering them in [`scripts/dataset_pipeline/brand_tokens.yaml`](../../scripts/dataset_pipeline/brand_tokens.yaml) with the `_PP` suffix (e.g. `Marshall: MARSHALL_PP`).

Tokens are applied **just-in-time** by the pipeline:
- `metadata.json[*].prompt` always carries the **raw** character name (`"Chase, a happy animated dog…"`).
- `render_gallery_for_dataset.py` and `emit_trainer_csv.py` substitute via `apply_brand_tokens()` at write-time, drawing from the YAML map.
- The Gemma text encoder sees only the substituted text. The persisted `precomputed/text_prompt_conditions/*.pt` files are encodings of `CHASE_PP`-bearing captions.

**Inference / validation prompts must use the substituted form.** Typing `Chase` at inference will hit the base model's prior, not the LoRA. Validation prompts inside trainer configs (`packages/ltx-trainer/configs/*.yaml`) must use `CHASE_PP` / `SKYE_PP` to invoke the trained adapter.

---

## Why this form (and not the alternatives)

| Candidate | Why rejected |
|---|---|
| `<chase_pp>` | Gemma tokenizes `<` as one token + `chase_pp` as more; the angle brackets aren't special. Equivalent to `chase_pp` plus stray punctuation tokens. |
| `[CHASE]` | Same as above with `[` and `]`. Brackets are also common in transcript markup → potential collision. |
| `chasepawpatrol` | Tokenizes inconsistently across capitalizations and word boundaries; Gemma's BPE merges shift with surrounding context. Reproducibility hazard. |
| `sks chase` | The DreamBooth convention. **`chase` itself collides with the English verb "chase"** — defeats the entire point of isolating from priors. |
| True new token (Option B) | Requires `tokenizer.add_tokens()` + `model.resize_token_embeddings()` + persisting the modified tokenizer alongside every checkpoint. Hard project constraint at the time was *no fork of the LTX-Video-Trainer*. Flagged as a possible follow-up if Step 4 (verbatim instruction following) needs the stronger isolation. |
| `CHASE_PP` (chosen) | Multi-subword pattern (Gemma tokenizes as ~4 subwords: `CH`, `ASE`, `_`, `PP`); no whole-word collision with priors; the underscore + double-letter suffix forms a sequence that is exceedingly rare in pre-training corpora; deterministic. **Implementable without any trainer change.** |

---

## How the "drawer" actually works

`CHASE_PP` is **not** a single token in Gemma's vocabulary — there is no entry `[CHASE_PP] → ID 50345` anywhere. The LoRA does not learn a new embedding vector. Instead:

1. Gemma's tokenizer splits `CHASE_PP` into ~4 ordinary subword tokens.
2. The text encoder produces a stable, reproducible sequence of hidden states for those 4 tokens (deterministic given the surrounding text).
3. The LoRA, trained on ~600 chase clips with this 4-token sequence in every caption, learns in its **cross-attention layers** to associate that exact sequence with the visual concept of Paw Patrol's Chase.
4. At inference time, prompts containing `CHASE_PP` produce the same 4-token sequence → cross-attention fires → LoRA renders Chase.

This is the **trigger-word LoRA pattern**, common in AnimateDiff / SDXL LoRA work. It is a softer mechanism than a true new-token registration would be, but it requires zero changes to the trainer or the model architecture.

---

## Consequences

### Positive

- **Zero fork of LTX-Video-Trainer.** All changes are in the dataset pipeline only.
- **No collision with priors.** `CHASE_PP` shares no whole-word tokens with the verb "chase", and no whole-word with German Shepherd / Cockapoo / breed descriptors.
- **Configurable without rebuild.** Changing the token map (e.g. `Chase: CHASE_BRAND`) requires re-encoding text conditions only. Video latents are preserved (they don't depend on caption text).
- **Persistent metadata stays human-readable.** Anyone reading `metadata.json` sees `"Chase, a happy animated dog…"` — no obfuscated identifiers in the source-of-truth files.

### Negative — be honest about these

- **Subword leakage.** The component subwords (`CH`, `ASE`, etc.) appear in many other words; the LoRA's "drawer" is therefore softer than Option B would give. Expected effect: prompts mentioning unrelated `CH`-prefixed words (chemistry, charming, …) may receive slight spurious modulation. We have not measured this empirically.
- **No orthogonal embedding.** A true new token starts orthogonal to the existing concept space; our 4-subword pattern doesn't. The LoRA must overcome whatever residual prior exists in the constituent subwords.
- **Manual discipline required at inference.** Anyone writing inference prompts by hand has to remember to type `CHASE_PP`. Typing `Chase` silently hits the base model. Mitigation: every gallery video burns the substituted form into the visible caption (so authoring against the gallery's text reinforces the convention); validation prompts in trainer configs are audited (see ADR-001 follow-up tasks).
- **Older configs still reference bare `CHASE` / `SKYE`.** The pre-rebuild configs (`chase_exp1_highcap.yaml`, `skye_exp{1..6}_*.yaml`, `goldenPLUS150_*.yaml`, etc.) used uppercase-bare. Any LoRA produced from those configs is trained against `CHASE` and is incompatible with `CHASE_PP` prompting at inference. **Action**: those configs are updated to `CHASE_PP` / `SKYE_PP` in the same commit as this ADR.
- **Breed descriptors ("German Shepherd", "Cockapoo") still appear in older validation prompts.** These dilute the brand-isolation goal. Flagged for content-level review; this ADR covers token format only.

### Neutral

- The `--lora-trigger` CLI flag of `process_dataset.py` (which would prepend a trigger word to every caption automatically) is **not used**. We bake substitution into the caption proper instead. This is functionally equivalent for our use case but means we don't depend on a trainer feature that may change.

---

## Follow-up tasks (out of scope for this ADR; tracked for later)

1. **Option B evaluation** — if Step 4 (verbatim instruction following) shows the soft-drawer is leaking too much from priors, evaluate adding `<chase_pp>` and `<skye_pp>` as true new tokens (tokenizer + embedding-matrix modification + persist with checkpoints). Out of scope until Step 1 ships a baseline.
2. **Empirical probe of subword leakage** — pick 50 prompts containing the constituent subwords (`CH…`, `…ASE`, `..._PP`) but not the full `CHASE_PP` sequence; render with the trained LoRA and compare to the base model. Quantify the residual modulation.
3. **Audit older training configs for breed descriptors** — separate review pass on `validation.prompts` arrays in the legacy configs to remove or qualify "German Shepherd" / "Cockapoo" mentions where they describe the brand character. Out of scope here; flagged for content review.
4. **Inference-time enforcement** — add a `--strict-brand-tokens` flag to inference scripts that errors out (or warns) if a known character name appears bare in the prompt. Mechanical guardrail against hand-authored regressions.
