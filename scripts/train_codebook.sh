#!/bin/bash
set -e

torchrun --nproc_per_node 1 -m training.main \
    --save-frequency 1 \
    --zeroshot-frequency 1 \
    --log-every-n-steps 1 \
    --train-data "/path/to/cc12m/{00000..01242}.tar::/path/to/cc3m/{00000..00331}.tar" \
    --train-num-samples 12825420 \
    --logs "/path/to/logs" \
    --dataset-type webdataset \
    --imagenet-val "/path/to/imagenet/val/" \
    --warmup 3600 \
    --batch-size 512 \
    --wd 0.5 \
    --epochs 25 \
    --workers 4 \
    --accum-freq 4 \
    --model ViT-B-32 \
    --name clip_debug \
    --save-most-recent \
    --resume latest \
    --precision amp_bfloat16 \
    --seed 0 \
    --grad-checkpointing \
    --local-loss \
    --gather-with-grad \
    --override-model-config '{"use_codebook":true}'
