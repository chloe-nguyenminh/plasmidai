#!/bin/bash
#SBATCH --nodes 1
#SBATCH --gres=gpu:4
#SBATCH --tasks-per-node=4
#SBATCH --cpus-per-task=8
#SBATCH --mem=32GB
#SBATCH --time=1-00:00
#SBATCH --output=logs/%N-%j.out

module load StdEnv/2023 python/3.10 scipy-stack
virtualenv --no-download $SLURM_TMPDIR/env
source $SLURM_TMPDIR/env/bin/activate
pip install --no-index "torch<2.3" pytorch_lightning wandb "pydantic<2" einops scipy pandas biopython mamba_ssm
pip install $HOME/wheels/pydantic_cli-4.3.0-py3-none-any.whl

export REPO_ROOT=~/code/plasmid-ai
cd $REPO_ROOT

wandb offline

export TORCH_NCCL_BLOCKING_WAIT=1

srun python -m src.experimental.llm.train \
    --accelerator=gpu --devices=4 \
    --precision=bf16-mixed \
    --batch_size=40 --num_workers=4 \
    --enable_fused_add_norm \
    --enable_wandb --wandb_dir="$REPO_ROOT/logs" \
    --enable_checkpoint --checkpoint_dir="$REPO_ROOT/checkpoints/$(date +'%M-%H-%d-%m-%Y')" \
    --enable_progress_bar \
    --max_epochs=400 --train_steps_per_epoch=500 --val_steps_per_epoch=500 --scheduler_span=50000
