import functools

import torch
import torch.nn as nn

try:
    from mamba_ssm.ops.triton.layernorm import RMSNorm
except ImportError:
    RMSNorm = None


def linear_warmup(step, warmup):
    return min(step, warmup) / warmup


def build_optimizer_and_scheduler(model, lr, betas, wd, warmup, **optim_kwargs):
    params = []
    params_no_wd = []

    for name, p in model.named_parameters():
        *attrs, name = name.split(".")

        # Get parent module
        parent = model
        for k in attrs:
            parent = getattr(parent, k)

        # Bucket parameters depending on whether they need weight decay
        if isinstance(parent, (nn.LayerNorm, RMSNorm)) or (name == "bias"):
            params_no_wd.append(p)
        elif getattr(p, "_no_weight_decay", False):  # some Mamba params.
            params_no_wd.append(p)
        else:
            params.append(p)

    optimizer = torch.optim.AdamW(
        params=[
            {"params": params, "weight_decay": wd},
            {"params": params_no_wd, "weight_decay": 0.0},
        ],
        lr=lr, betas=betas,
        **optim_kwargs,
    )

    warmup_fn = functools.partial(linear_warmup, warmup=warmup)
    warmup_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=warmup_fn)

    return optimizer, warmup_scheduler
