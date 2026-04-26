# Benchmark Handoff — Phase 4 LoRA Evaluation

**Audience:** A new Claude session inheriting the benchmark work.
**Last updated:** 2026-04-26.
**Companion docs:** [PHASE4_README.md](PHASE4_README.md) for full Phase-4 plan and training-side state.

---

## TL;DR — What's running, where, what's queued

| Machine | Job | What | W&B / Output |
|---|---|---|---|
| **34.61.89.219** | training | Phase 2 curriculum (rank 32, full re-captioned 472, warm-started from Phase 1) | [qr9ohs8c](https://wandb.ai/shayzweig-smiti-ai/LTX2_lora/runs/qr9ohs8c) |
| **34.56.137.11** | training | Rank-128 ablation (rank 128 from scratch, full re-captioned 472) | [kjy9r8wq](https://wandb.ai/shayzweig-smiti-ai/LTX2_lora/runs/kjy9r8wq) |
| **34.132.149.33** | training | Rank-32 from-scratch (warm-start ablation, full re-captioned 472) | [r864wj4l](https://wandb.ai/shayzweig-smiti-ai/LTX2_lora/runs/r864wj4l) |
| **35.225.196.76** | **benchmark** | BM_v1 re-run with corrected per-scene 10s × dimensions (middle + final ckpts only) | output to `lora_results/{goldenPlus150_version1,full_episods}/<exp>_bm_v2/` |

The 4 W&B runs all live in project `LTX2_lora` under `shayzweig-smiti-ai`.

---

## Three benchmark scripts at `/Users/efrattaig/projects/sm/LTX-2/`

| Script | Pipeline | Resolution | Duration | Scenes |
|---|---|---|---|---|
| `run_bm_v1_lora.py` | low-level `ValidationSampler` (single-stage diffusion) | per-scene from `config.json` (after the recent patch) | per-scene from `config.json` (after the patch) | 7 BM_v1 scenes |
| `run_chase_benchmark.py` | `TI2VidTwoStagesHQPipeline` (high-quality two-stage) | 832×960 | 10s (257 frames @ 24fps) | 5 Chase scenes |
| `run_skye_benchmark.py` | `TI2VidTwoStagesHQPipeline` | 768×1024 | 10s (257 frames) | 10 Skye scenes (5 standard + 5 "of_*" overfit) |

### `run_bm_v1_lora.py` — important context

Three patches landed during this session — make sure your machine has the latest commit (`5bacec5` or newer):

1. **Text encoder API mismatch (commit `6cd7c99`):** Old code called `text_encoder(prompt) → (video, audio, mask)`. After ltx-core refactor, must use `text_encoder.encode(prompt) → (hidden_states, mask)` followed by `embeddings_processor.process_hidden_states(hs, mask)`.

2. **YAML loader (commit `2242b86`):** `_load_lora_config_from_exp` must use `yaml.unsafe_load`, not `safe_load` — the trainer-saved `training_config.yaml` contains `!!python/tuple` tags that `safe_load` rejects.

3. **Hardcoded duration (commit `5bacec5`):** Used to use `BM_FRAMES = 97` (~4 s) and `BM_WIDTH=960, BM_HEIGHT=544` for every scene. Now reads `duration` and `dimensions` from each scene's `config.json`, snapping width/height to multiples of 32 and frames to `% 8 == 1`. Falls back to old constants if the config lacks those fields.

### `run_chase_benchmark.py` / `run_skye_benchmark.py`

These were already correct (10 s × proper dims). Skye's 5 "of_*" scenes need `inputs/overfit_bm/scene_{27,41,58,66,189}_*.png` to exist — script aborts upfront if any are missing. That dir has been rsync'd everywhere we ran skye.

---

## Benchmark exp-dir convention

Each script auto-discovers checkpoints in `<exp_dir>/checkpoints/lora_weights_step_*.safetensors`. To benchmark a specific subset:

1. **Create a new exp dir** (e.g., `outputs/myexp_benchmark/checkpoints/`)
2. **Copy or symlink** the desired checkpoints into it
3. **Optionally rename them** with cumulative-step naming so the benchmark output is naturally labeled by total training progress (we did this for goldenPLUS150 — files renamed e.g. `lora_weights_run2_step_07000` → `lora_weights_step_10000` for the cumulative-10k checkpoint)
4. **Copy `training_config.yaml`** from the original exp dir into the benchmark exp dir (the script reads LoRA config from there)
5. **Run** `uv run python run_bm_v1_lora.py --exp-dir outputs/myexp_benchmark --out-base lora_results/...`

Use `--steps STEP1 STEP2 ...` to filter to just specific checkpoints, or `--last-only` for just the final.

---

## Where each model's checkpoints live

| Experiment | Checkpoint location | Naming convention |
|---|---|---|
| goldenPLUS150 (5 cumulative ckpts: 10k/15k/25k/35k/40k) | 34.61.89.219:`outputs/goldenPLUS150/checkpoints/` | `run1_*` (cumulative 0-3k), `run2_*` (3-10k), unprefixed (10k-40k from run3 — these are the BIG step counter from run3's own counting). Cumulative-renamed copies live in 34.56.137.11/35.225.196.76:`outputs/goldenPLUS150_benchmark/` |
| full_cast_exp1_season1 (4 cumulative ckpts: 3.5k/6k/8.5k/10k) | 34.56.137.11:`outputs/full_cast_exp1_season1/checkpoints/` | `run2_*` (cumulative 1-3.5k), unprefixed (3.5k-10k from run3). Cumulative-renamed copies on 35.225.196.76:`outputs/full_cast_exp1_benchmark/` |
| Phase 2 (running) | 34.61.89.219:`outputs/goldenPLUS150_v2_phase2/checkpoints/` | step counter starts at 0; this counts on top of warm-start at cumulative-15k effective |
| Rank-128 (running) | 34.56.137.11:`outputs/goldenPLUS150_v2_rank128/checkpoints/` | step counter starts at 0; from scratch |
| Rank-32-scratch (running) | 34.132.149.33:`outputs/goldenPLUS150_v2_rank32_scratch/checkpoints/` | step counter starts at 0; from scratch |

Backup directories:
- `outputs/goldenPLUS150/checkpoints_v1_generic_backup/` — never written to in this session, may be empty/absent
- `data/full_cast_combined_preprocessed/conditions_v1_generic_backup/` (on 34.61.89.219) — 136 generic-caption .pt files preserved before re-captioning
- `data/full_cast_exp1_preprocessed/conditions_v1_generic_backup/` (on 34.56.137.11) — same backup, 34.56.137.11 side

---

## Output path conventions

The user wants results landing at these **local paths**:
- `/Users/efrattaig/projects/sm/LTX-2/lora_results/goldenPlus150_version1/` — all goldenPLUS150 BM_v1 benchmark outputs
- `/Users/efrattaig/projects/sm/LTX-2/lora_results/full_episods/` — all full_cast_exp1_season1 BM_v1 benchmark outputs (this matches `DEFAULT_OUT_BASE` in run_bm_v1_lora.py)
- `/Users/efrattaig/projects/sm/LTX-2/lora_results/chase_lora/` — Chase character-specific benchmark
- `/Users/efrattaig/projects/sm/LTX-2/lora_results/skye_lora/` (or `output/benchmarks/` from skye script's default) — Skye character-specific benchmark

Each contains subdirs per `<exp_name>` (e.g. `goldenPLUS150_benchmark/`), then `_base/` for no-LoRA and `step_NNNNN/` per checkpoint with `<scene>.mp4` (comparison) and `<scene>_lora.mp4` (LoRA only).

After a benchmark finishes on a remote machine, **rsync to local** with:
```bash
rsync -avz efrat_t_smiti_ai@<server>:/home/efrat_t_smiti_ai/projects/LTX-2/lora_results/<dir>/ \
  /Users/efrattaig/projects/sm/LTX-2/lora_results/<dir>/
```

---

## Common operational rules (from CLAUDE.md and what we learned this session)

1. **Always tee training/benchmark output to `/tmp/ltx_current.log`** so `tail -f` works for the user.

2. **Wrap nohup'd jobs in `bash -c '... | tee ...'`** to prevent the SSH-tee SIGPIPE bug:
   ```bash
   ssh server "cd /home/.../LTX-2 && nohup bash -c 'env PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True ... | tee /tmp/ltx_current.log' > /tmp/nohup.out 2>&1 &"
   ```
   The `nohup` only protects what comes immediately after it — wrapping in `bash -c` with the pipe inside means *the entire pipeline* is SIGHUP-immune.

3. **Configs are `.gitignored`** — must `scp` them to each server, can't push via git.

4. **Code goes through git** — push to `Smiti-AI/LTX-2`, then `git pull` on the server. Your push uses gh credentials (run `gh auth setup-git` once if git asks for credentials).

5. **Server-to-server rsync** uses a temp ed25519 key:
   ```bash
   ssh-keygen -t ed25519 -N '' -f ~/.ssh/temp_xfer
   # Add the .pub line to the source server's ~/.ssh/authorized_keys
   rsync -avz -e 'ssh -i ~/.ssh/temp_xfer -o StrictHostKeyChecking=no' src dst
   ```
   GCP internal network gives ~300-380 MB/s.

6. **Always check for zombie GPU processes** before launching:
   ```bash
   ssh server "ps aux | grep -E 'train.py|run_bm' | grep -v grep"
   nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader
   ```

7. **The `sigma_tracker.py` patch (commit `4741ee0`)** is now on Smiti-AI/main. Any training run started after that commit gets per-σ-bucket loss logging. Currently running rank-128 (kjy9r8wq) was launched before — does NOT have buckets. Phase 2 (qr9ohs8c) and Rank-32-scratch (r864wj4l) DO have them.

---

## Auto-discovery hazard: step counter resets on resume

When `load_checkpoint` is set in a training config, the trainer **resets `_global_step` to 0** and starts a new run inside the same `output_dir`. The checkpoint files saved inside that resumed run use the new step counter, *not* the cumulative step. They overwrite previous-run files with the same step number unless renamed first.

We hit this twice:
- goldenPLUS150 has `run1_step_*` (0-3k), `run2_step_*` (3-10k), unprefixed `step_*` (10-40k from run3) — 30k checkpoints total.
- full_cast_exp1_season1 has `run2_step_*` (1-3.5k), unprefixed `step_*` (3.5-10k from run3) — 13 checkpoints total.

For benchmarking, we set up parallel "benchmark" exp dirs with cumulative-step renaming so the output naturally reflects total training progress.

---

## VLM analysis viewer pipeline

Script: `auto_val/build_phase4_viewer.py` (committed but not yet executed end-to-end).

When all current jobs finish:
1. Rsync results from each remote to local `lora_results/`
2. Run `cd auto_val && uv run python build_phase4_viewer.py`
3. For each of 17 scenes (7 BM_v1 + 5 chase + 5 skye), it picks 3 videos:
   - **150** = goldenPLUS150 cumulative-10k (best per diagnosis, before low-σ overfitting)
   - **episode** = full_cast_exp1_season1 cumulative-10k (final)
   - **base** = LTX-2.3 base, no LoRA
4. Runs `vlm_pipeline.run --mode cheap` (Gemini Flash) on each video → produces report.json with score/verdict/headline
5. Writes per-scene `outputs_vlm_compare_phase4_<scene>/manifest.json` + `viewer.html`
6. Total VLM cost: ~$5 for 51 calls

The user wants results visible via the auto_val viewer at `auto_val/index.html` (top-level landing page) → individual scene comparisons in `outputs_vlm_compare_phase4_<scene>/viewer.html`. To serve locally:
```bash
cd /Users/efrattaig/projects/sm/LTX-2/auto_val && ./serve_viewer.sh
# → http://localhost:8000
```

---

## Open methodological note

The user flagged that the comparison between Phase 2 (warm-started) and Rank-128 (from scratch) is confounded — they differ in *both* rank and warm-start. The new arm on 34.132.149.33 (rank 32 from scratch, same data) cleans this up:
- Phase 2 vs **Rank-32-scratch** isolates the warm-start effect
- Rank-32-scratch vs **Rank-128** isolates the rank effect
This 2x2 (with the warm-started+rank-128 cell impossible due to shape mismatch) is the cleanest factorial we can run with available compute.

---

## What a new session must do (in priority order)

1. **Read `PHASE4_README.md`** for the full training plan and decision tree.
2. **Check live state** of all 4 jobs (`ssh ... ps aux`, `nvidia-smi`, `tail /tmp/ltx_current.log`) before doing anything.
3. **When the BM_v1 re-run on 35.225.196.76 finishes**, rsync results to the two local dirs (`goldenPlus150_version1` and `full_episods`). The new outputs land in subdirs `goldenPLUS150_bm_v2/` and `full_cast_exp1_bm_v2/`.
4. **When training runs finish**, set up benchmark exp dirs with the new checkpoints and run the appropriate benchmarks. For each new run we want chase + skye (character fidelity) at minimum, BM_v1 if compute is available.
5. **Run `auto_val/build_phase4_viewer.py`** once all benchmarks are in.
