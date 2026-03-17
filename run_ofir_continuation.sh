#!/bin/bash
# Video-to-Video continuation: uses full reference video for spatial consistency
# Full reference helps reduce pass-through artifacts (model sees correct spatial layout)
#
# Output: input/ofir/res/robot_closet_continuation_4s.mp4

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Model paths
CHECKPOINT="${CHECKPOINT:-$SCRIPT_DIR/models/ltx-2.3-22b-dev.safetensors}"
GEMMA_ROOT="${GEMMA_ROOT:-$SCRIPT_DIR/models/gemma-3-12b-it-qat-q4_0-unquantized}"

# Input / output
REF_VIDEO="$SCRIPT_DIR/input/ofir/2B_post_trained_robot_closet_14b_v2w_s42_g4_v2w_seed42_steps35.mp4"
OUTPUT_DIR="$SCRIPT_DIR/input/ofir/res"
OUTPUT="$OUTPUT_DIR/robot_closet_continuation_4s.mp4"
mkdir -p "$OUTPUT_DIR"

# Prompt: Physical realism FIRST (model often ignores constraints buried in text)
# Use full reference video (V2V) for better spatial consistency—reduces pass-through artifacts
PROMPT='CRITICAL PHYSICAL CONSTRAINT: Solid objects cannot pass through each other. The gripper fingers STOP at the handle surface—they wrap around the exterior, they do NOT penetrate or pass through. The right robot gripper closes around the cabinet door handle from the outside only. Gripper approaches the center of the right closet door handle, touches its surface, and closes around it. Real-world physics: contact, no intersection. Static camera, bright indoor scene, two robotic arms facing a closed white closet with two tall doors, vertical metallic handles. Black claw-like grippers with two parallel fingers. Left arm, both doors, camera: frozen. Only the right gripper moves.'
NEGATIVE_PROMPT='Passing through, penetration, gripper through handle, intersection, clipping, gripper inside handle, unreal physics, impossible physics, objects through each other, left arm moving, left gripper moving, door opening, door moving, camera movement, camera shake, pan, zoom, both arms moving, ugly, motion blur, over-saturation, shaky, low resolution, grainy, pixelated, poorly lit, artifacting, color banding, jump cuts, visual noise, flickering.'

# 4 seconds: num_frames must be k*8+1 -> 97 frames @ 25fps = 3.88s
NUM_FRAMES=97
FRAME_RATE=25.0

# Lower resolution
WIDTH=640
HEIGHT=352
NUM_INFERENCE_STEPS=40
GUIDANCE_SCALE=4.0
STG_SCALE=1.0
STG_BLOCKS=29
STG_MODE="stg_av"
SEED=42

echo "=============================================="
echo "Video Continuation (V2V - full reference)"
echo "=============================================="
echo "Reference video: $REF_VIDEO"
echo "Output:    $OUTPUT"
echo "Duration:  ~4 seconds ($NUM_FRAMES frames @ ${FRAME_RATE}fps)"
echo "Quality:   $NUM_INFERENCE_STEPS steps, ${WIDTH}x${HEIGHT}"
echo "=============================================="

cd packages/ltx-trainer
uv run python scripts/inference.py \
    --checkpoint "$CHECKPOINT" \
    --text-encoder-path "$GEMMA_ROOT" \
    --prompt "$PROMPT" \
    --negative-prompt "$NEGATIVE_PROMPT" \
    --reference-video "$REF_VIDEO" \
    --output "$OUTPUT" \
    --width "$WIDTH" \
    --height "$HEIGHT" \
    --num-frames "$NUM_FRAMES" \
    --frame-rate "$FRAME_RATE" \
    --num-inference-steps "$NUM_INFERENCE_STEPS" \
    --guidance-scale "$GUIDANCE_SCALE" \
    --stg-scale "$STG_SCALE" \
    --stg-blocks $STG_BLOCKS \
    --stg-mode "$STG_MODE" \
    --seed "$SEED" \
    --device cuda \
    --skip-audio

echo ""
echo "✓ Video saved to: $OUTPUT"
