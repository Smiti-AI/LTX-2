# My Notes — Video Generation Project

> This is my personal file. I use it to communicate with Claude, track what I understand,
> and write questions or notes to myself.

---

## What I'm Trying to Do

I want to generate high-quality cartoon videos locally on my machine —
the same quality I get from the external paid API (Fal).

Once I get good results locally, I'll train the model to do something specific
(generate Skye from Paw Patrol saying custom birthday messages, etc.).

The rule: **don't start training until local quality matches the API.**

---

## The Two Scripts I Should Care About

### The API (external, paid)
**`run_bm_v1_ltx23_fast.py`** — sends my image + prompt to Fal's servers and gets a video back.
This is the gold standard. The result is good quality.

### My local script (what I just wrote)
**`run_skye_like_api.py`** — runs the same AI model on my own machine.
This is what I should use and compare against the API.

**To run it:**
```bash
uv run python run_skye_like_api.py \
    --scene inputs/BM_v1/benchmark_v1/skye_helicopter_birthday_gili
```

---

## Why Was Local Quality Bad Before?

Three problems with the old scripts:

### Problem 1 — Wrong resolution (the biggest one)
`run_skye_video_v2.sh` was generating videos at 224×416 pixels.
That's tiny — about the size of a postage stamp.
The API generates at 1920×1080 (full HD).
This was a leftover "fast test" setting that was never changed back.

### Problem 2 — Too few steps
The old script used 3 denoising steps. The API uses ~15.

**What is a "denoising step"?**
AI video generation starts from pure random noise and gradually cleans it up,
step by step, until it becomes a real video. More steps = more refined result.
3 steps is like stopping halfway through — the result looks blurry and unfinished.

### Problem 3 — Wrong pipeline
The old script used a simpler, single-stage process.
The API uses a smarter two-stage process (explained below).
Even with the same settings, the single-stage process produces worse results.

---

## How the AI Actually Generates the Video

### What is "diffusion"?
The AI doesn't draw the video from scratch. It starts with a completely random
noisy image (like TV static) and slowly removes the noise, step by step,
guided by your prompt and your start image — until a real video appears.

This process is called **diffusion** (the noise "diffuses" away).

### The two-stage process (what the API and my new script both use)

```
Your image + your prompt
         ↓
  STAGE 1 — Small size first
  Generates the video at half resolution (960×544 pixels)
  Takes 15 steps
  The model figures out: what happens? who moves? how?
         ↓
  STAGE 2 — Sharpen and enlarge
  Doubles the size to full resolution (1920×1088 pixels)
  Takes only 4 steps (it just sharpens — doesn't re-invent)
         ↓
  Final video at full HD quality
```

**Why two stages instead of one?**
Going straight to full HD from scratch would take forever and require
a huge amount of GPU memory. Stage 1 does the "thinking" at a small size.
Stage 2 just makes it sharp and big. Much faster, much better result.

---

## What is a LoRA?

A LoRA is a small add-on file that modifies how the AI model behaves,
without changing the main model itself. Think of it like a plugin or a filter.

**The LoRA I'm using:** `ltx-2.3-22b-distilled-lora-384.safetensors`

This is **not** a style LoRA (it doesn't make videos look like Paw Patrol).
It's a **speed LoRA** — it was trained to make the model produce high quality
in fewer steps (especially in Stage 2, which only has 4 steps).

It's applied twice:
- In Stage 1 at **strength 0.25** — gentle nudge, Stage 1 already has 15 steps
- In Stage 2 at **strength 0.50** — stronger push, Stage 2 only has 4 steps and needs help

---

## Every Parameter — In Plain Language

One thing to know first: the Fal API only lets you control a few things.
Most settings are locked inside and you can't touch them.
My local script uses the exact same locked values — that's the whole point.

Below, each parameter shows: what it does, what value is used, and whether
the API lets you change it or not.

---

### Resolution — how many pixels the video has

**Local: 1920 × 1088 — API: 1920 × 1080 — Can you change it in the API? Yes**

You can tell the API to output at 1080p, 1440p, or 2160p.
Locally, the height comes out as 1088 instead of 1080 because the math
requires a number divisible by 64 — but the videos look identical.

---

### Duration — how long the video is

**Local: you choose — API: you choose — Can you change it in the API? Yes**

Both locally and in the API you pick the duration: 6, 8, 10 seconds (or up to 20).

---

### FPS — how smooth the video is (frames per second)

**Local: 25 — API: 25 — Can you change it in the API? Yes**

The API lets you pick 24, 25, 48, or 50. Both default to 25.

---

### Steps — how many times the AI "cleans up" the noise

**Local: 15 — API: 15 — Can you change it in the API? No**

The AI starts from random noise and cleans it up step by step.
15 steps is what the API uses internally. You can't change this in the API.
(And 15 is already good quality — changing it locally is not recommended.)

---

### CFG (Guidance Scale) — how strictly the AI follows your prompt

Imagine you hired an artist and you're directing them:
- Low number (1–2) → they mostly do their own thing, barely follow your words
- Medium number (3) → they follow your direction but not robotically
- High number (7+) → they follow your words exactly, but the result can look unnatural

**Video CFG — Local: 3.0 — API: 3.0 — Can you change it in the API? No**
The start image already tells the AI what to draw, so the prompt just needs gentle guidance.

**Audio CFG — Local: 7.0 — API: 7.0 — Can you change it in the API? No**
There's no image for the audio to anchor to, so it needs stricter instruction.

---

### Negative Prompt — what you want the AI to avoid

You give the AI two texts: what you want (your prompt) and what you don't want
(the negative prompt). The AI steers toward one and away from the other.

**Local: full built-in list — API: same list — Can you change it in the API? No**

The old script (`run_skye_v0.py`) had this empty — meaning "avoid nothing."
The new script uses the library's built-in list: blur, distortion, bad anatomy,
artifacts, wrong colors, bad lip sync, etc. This is the single biggest fix
and the main reason the new script produces better results.

---

### Rescale — tones down over-vivid colors after CFG

CFG sometimes makes the video too colorful and harsh. Rescale pulls it back.

**Video rescale — Local: 0.45 — API: 0.45 — Can you change it in the API? No**

**Audio rescale — Local: 1.0 (off) — API: 1.0 (off) — Can you change it in the API? No**

---

### A2V / V2A — how much audio and video shape each other

The model generates audio and video at the same time.
A2V controls how much the audio pulls on the video (helps lip sync).
V2A controls how much the video pulls on the audio (sound matches what's on screen).

**Both — Local: 3.0 — API: 3.0 — Can you change it in the API? No**

These are completely hidden inside the API. My script uses the same value. They match.

---

### STG — an extra quality technique

STG deliberately disturbs the AI mid-generation to push it toward better quality.
It works well in some modes but makes things worse in the two-stage HQ pipeline.

**Local: OFF — API: OFF — Can you change it in the API? No**

Both local and the API run with STG turned off. They match. This is correct.

---

### Image Strength — how strictly the first frame matches your input image

**Local: 1.0 — API: 1.0 — Can you change it in the API? No**

1.0 means the first frame of the video will exactly match the image you give it.
A lower number like 0.9 gives the AI a tiny bit of freedom — but 1.0 is what the API uses.

---

## What Changed from `run_skye_v0.py` to `run_skye_like_api.py`

| What changed | Old (`run_skye_v0.py`) | New (`run_skye_like_api.py`) |
|:---|:---|:---|
| Negative prompt | empty — useless | full library default — works properly |
| Scene loading | manual, no config | pass `--scene folder/` and it reads everything |
| End frame | not supported | supported — AI will transition from start to end image |
| Parameters | hardcoded | all adjustable from command line |

---

## Before I Start Training — Checklist

- [ ] Run `run_skye_like_api.py` on at least one scene
- [ ] Run the same scene through the Fal API (`run_bm_v1_ltx23_fast.py`)
- [ ] Compare the two videos side by side
- [ ] If they look similar — I'm ready to think about training
- [ ] If local still looks worse — investigate further before training

---

## Questions / Notes to Self

> Write your questions and observations here. Claude will read this file
> and answer in context.

