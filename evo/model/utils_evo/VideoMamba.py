# Copyright (c) 2015-present, Facebook, Inc.
# All rights reserved.
import os
import torch
import torch.nn as nn
from functools import partial
from torch import Tensor
from typing import Optional
import torch.utils.checkpoint as checkpoint

# remember that this is einstein operation, which is the special fancy way of reshaping.
from einops import rearrange
from timm.models.vision_transformer import _cfg
from timm.models.registry import register_model
from timm.models.layers import trunc_normal_

from timm.models.layers import DropPath
from timm.models.vision_transformer import _load_weights

import math

from mamba_ssm.modules.mamba_simple import Mamba

try:
    from mamba_ssm.ops.triton.layernorm import RMSNorm, layer_norm_fn, rms_norm_fn
except ImportError:
    RMSNorm, layer_norm_fn, rms_norm_fn = None, None, None


MODEL_PATH = "your_model_path"
_MODELS = {
    "videomamba_t16_in1k": os.path.join(MODEL_PATH, "videomamba_t16_in1k_res224.pth"),
    "videomamba_s16_in1k": os.path.join(MODEL_PATH, "videomamba_s16_in1k_res224.pth"),
    "videomamba_m16_in1k": os.path.join(MODEL_PATH, "videomamba_m16_in1k_res224.pth"),
}


# !mamba block from the repository
class Block(nn.Module):
    def __init__(
        self,
        dim,
        mixer_cls,
        norm_cls=nn.LayerNorm,
        fused_add_norm=False,
        residual_in_fp32=False,
        drop_path=0.0,
    ):
        """
        Simple block wrapping a mixer class with LayerNorm/RMSNorm and residual connection"

        This Block has a slightly different structure compared to a regular
        prenorm Transformer torchblock.
        The standard block is: LN -> MHA/MLP -> Add.
        [Ref: https://arxiv.org/abs/2002.04745]
        Here we have: Add -> LN -> Mixer, returning both
        the hidden_states (output of the mixer) and the residual.
        This is purely for performance reasons, as we can fuse add and LayerNorm.
        The residual needs to be provided (except for the very first block).
        """
        super().__init__()
        self.residual_in_fp32 = residual_in_fp32
        self.fused_add_norm = fused_add_norm

        # ! this is the mixer class that was created.
        self.mixer = mixer_cls(dim)
        self.norm = norm_cls(dim)

        # ! https://stackoverflow.com/questions/69175642/droppath-in-timm-seems-like-a-dropout
        # ! https://arxiv.org/abs/1603.09382, so they randomly drop some layers
        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()
        if self.fused_add_norm:
            assert RMSNorm is not None, "RMSNorm import fails"
            assert isinstance(
                self.norm, (nn.LayerNorm, RMSNorm)
            ), "Only LayerNorm and RMSNorm are supported for fused_add_norm"

    def forward(
        self,
        hidden_states: Tensor,
        residual: Optional[Tensor] = None,
        inference_params=None,
        use_checkpoint=False,
    ):
        r"""Pass the input through the encoder layer.

        Args:
            hidden_states: the sequence to the encoder layer (required).
            residual: hidden_states = Mixer(LN(residual))
        """
        if not self.fused_add_norm:
            residual = (
                (residual + self.drop_path(hidden_states))
                if residual is not None
                else hidden_states
            )
            hidden_states = self.norm(residual.to(dtype=self.norm.weight.dtype))
            if self.residual_in_fp32:
                residual = residual.to(torch.float32)
        else:
            fused_add_norm_fn = (
                rms_norm_fn if isinstance(self.norm, RMSNorm) else layer_norm_fn
            )
            hidden_states, residual = fused_add_norm_fn(
                hidden_states if residual is None else self.drop_path(hidden_states),
                self.norm.weight,
                self.norm.bias,
                residual=residual,
                prenorm=True,
                residual_in_fp32=self.residual_in_fp32,
                eps=self.norm.eps,
            )
        if use_checkpoint:
            hidden_states = checkpoint.checkpoint(
                self.mixer, hidden_states, inference_params
            )
        else:
            hidden_states = self.mixer(hidden_states, inference_params=inference_params)
        return hidden_states, residual

    def allocate_inference_cache(self, batch_size, max_seqlen, dtype=None, **kwargs):
        return self.mixer.allocate_inference_cache(
            batch_size, max_seqlen, dtype=dtype, **kwargs
        )


def create_block(
    d_model,
    ssm_cfg=None,
    norm_epsilon=1e-5,
    drop_path=0.0,
    rms_norm=True,
    residual_in_fp32=True,
    fused_add_norm=True,
    layer_idx=None,
    bimamba=True,
    device=None,
    dtype=None,
):
    factory_kwargs = {"device": device, "dtype": dtype}
    if ssm_cfg is None:
        ssm_cfg = {}
    # ! this partial function enables you to create a new class from someone else's class.
    mixer_cls = partial(
        Mamba, layer_idx=layer_idx, bimamba=bimamba, **ssm_cfg, **factory_kwargs
    )
    norm_cls = partial(nn.LayerNorm if not rms_norm else RMSNorm, eps=norm_epsilon)

    # ! and then, after creating a new mixer, they will create a block
    block = Block(
        d_model,
        mixer_cls,
        norm_cls=norm_cls,
        drop_path=drop_path,
        fused_add_norm=fused_add_norm,
        residual_in_fp32=residual_in_fp32,
    )
    block.layer_idx = layer_idx
    return block


# https://github.com/huggingface/transformers/blob/c28d04e9e252a1a099944e325685f14d242ecdcd/src/transformers/models/gpt2/modeling_gpt2.py#L454
def _init_weights(
    module,
    n_layer,
    initializer_range=0.02,  # Now only used for embedding layer.
    rescale_prenorm_residual=True,
    n_residuals_per_layer=1,  # Change to 2 if we have MLP
):
    if isinstance(module, nn.Linear):
        if module.bias is not None:
            if not getattr(module.bias, "_no_reinit", False):
                nn.init.zeros_(module.bias)
    elif isinstance(module, nn.Embedding):
        nn.init.normal_(module.weight, std=initializer_range)

    if rescale_prenorm_residual:
        # Reinitialize selected weights subject to the OpenAI GPT-2 Paper Scheme:
        #   > A modified initialization which accounts for the accumulation on the residual path with model depth. Scale
        #   > the weights of residual layers at initialization by a factor of 1/√N where N is the # of residual layers.
        #   >   -- GPT-2 :: https://openai.com/blog/better-language-models/
        #
        # Reference (Megatron-LM): https://github.com/NVIDIA/Megatron-LM/blob/main/megatron/model/gpt_model.py
        for name, p in module.named_parameters():
            if name in ["out_proj.weight", "fc2.weight"]:
                # Special Scaled Initialization --> There are 2 Layer Norms per Transformer Block
                # Following Pytorch init, except scale by 1/sqrt(2 * n_layer)
                # We need to reinit p since this code could be called multiple times
                # Having just p *= scale would repeatedly scale it down
                nn.init.kaiming_uniform_(p, a=math.sqrt(5))
                with torch.no_grad():
                    p /= math.sqrt(n_residuals_per_layer * n_layer)


def segm_init_weights(m):
    if isinstance(m, nn.Linear):
        trunc_normal_(m.weight, std=0.02)
        if isinstance(m, nn.Linear) and m.bias is not None:
            nn.init.constant_(m.bias, 0)
    elif isinstance(m, nn.LayerNorm):
        nn.init.constant_(m.bias, 0)
        nn.init.constant_(m.weight, 1.0)


class Embedding(nn.Module):
    def __init__(self, input_channels, embed_dim):
        super().__init__()
        self.input_channels = input_channels
        self.embed_dim = embed_dim

        # few layers to project
        self.proj = nn.Linear(self.input_channels, self.embed_dim)

    def forward(self, x):
        x = self.proj(x)
        return x


class VisionMamba(nn.Module):
    def __init__(
        self,
        # img_size=(224, 224),
        # patch_size=4, # changing the patch size from 16 to be 4
        depth=24,
        output_evo_num_channels=4096,
        embed_dim=192,  # final number of channels.
        # channels=16, # which is the number of generated seqences by evo...
        # num_classes=1000, # yes, now I am going to use the cls token to determine which one is the best next token....right? or should I go by a distribution after the softmax...
        drop_rate=0.0,
        drop_path_rate=0.1,
        ssm_cfg=None,
        norm_epsilon=1e-5,
        initializer_cfg=None,
        fused_add_norm=True,
        rms_norm=True,
        residual_in_fp32=True,
        bimamba=True,
        # video
        kernel_size=1,
        num_tokens=64,  # length of each tokenized version of the
        fc_drop_rate=0.0,
        device=None,
        dtype=None,
        # checkpoint
        use_checkpoint=False,
        checkpoint_num=0,
    ):
        factory_kwargs = {"device": device, "dtype": dtype}  # follow MambaLMHeadModel
        super().__init__()
        self.residual_in_fp32 = residual_in_fp32
        self.fused_add_norm = fused_add_norm
        self.use_checkpoint = use_checkpoint
        self.checkpoint_num = checkpoint_num
        print(f"Use checkpoint: {use_checkpoint}")
        print(f"Checkpoint number: {checkpoint_num}")

        self.d_model = (
            self.num_features
        ) = self.embed_dim = embed_dim  # num_features for consistency with other models

        # *Notice that the first thing created is a patch embedding, to transform the image with convolutions
        self.output_evo_num_channels = output_evo_num_channels
        self.sequence_length = num_tokens
        # self.total_num_tokens = self.sequence_length * self.num_generated_sequences

        self.pos_drop = nn.Dropout(p=drop_rate)

        dpr = [
            x.item() for x in torch.linspace(0, drop_path_rate, depth)
        ]  # stochastic depth decay rule
        inter_dpr = [0.0] + dpr
        self.drop_path = (
            DropPath(drop_path_rate) if drop_path_rate > 0.0 else nn.Identity()
        )
        # mamba blocks
        # ! THIS IS WHERE It creates the mamba blocks, uses list comprehension to create as many mamba blocks as depth wants.
        self.layers = nn.ModuleList(
            [
                create_block(
                    embed_dim,
                    ssm_cfg=ssm_cfg,
                    norm_epsilon=norm_epsilon,
                    rms_norm=rms_norm,
                    residual_in_fp32=residual_in_fp32,
                    fused_add_norm=fused_add_norm,
                    layer_idx=i,
                    bimamba=bimamba,
                    drop_path=inter_dpr[i],
                    **factory_kwargs,
                )
                for i in range(depth)
            ]
        )

        # output head
        self.norm_f = (nn.LayerNorm if not rms_norm else RMSNorm)(
            embed_dim, eps=norm_epsilon, **factory_kwargs
        )

        # original init
        self.apply(segm_init_weights)
        # I am not using the classification head anyways.
        # self.head.apply(segm_init_weights)

        # adding positional embedding, within each sequence,
        self.pos_embed = nn.Parameter(
            torch.zeros(1, self.sequence_length, self.embed_dim)
        )
        # for each different sequence
        # self.temporal_pos_embedding = nn.Parameter(torch.zeros(1, self.num_generated_sequences, self.embed_dim))
        trunc_normal_(self.pos_embed, std=0.02)

        # customized embedding to match the patch embedding
        self.embedding = Embedding(self.output_evo_num_channels, self.embed_dim)

        # mamba init
        self.apply(
            partial(
                _init_weights,
                n_layer=depth,
                **(initializer_cfg if initializer_cfg is not None else {}),
            )
        )

    def allocate_inference_cache(self, batch_size, max_seqlen, dtype=None, **kwargs):
        return {
            i: layer.allocate_inference_cache(
                batch_size, max_seqlen, dtype=dtype, **kwargs
            )
            for i, layer in enumerate(self.layers)
        }

    @torch.jit.ignore
    def no_weight_decay(self):
        return {"pos_embed", "cls_token", "temporal_pos_embedding"}

    def get_num_layers(self):
        return len(self.layers)

    @torch.jit.ignore()
    def load_pretrained(self, checkpoint_path, prefix=""):
        _load_weights(self, checkpoint_path, prefix)

    def forward_features(self, x, inference_params=None):
        # increasing the number of channels
        # reshape the input, cancel that
        # x = rearrange(x, ' b c t -> (b t) c')
        x = self.embedding(x)
        x = rearrange(x, "(b t) c -> b t c", t=self.sequence_length)

        # 16, 64 tokens per sequences, 16 sequences -> 192 channels after,

        B, T, C = x.shape
        # x = rearrange(x, 'b c t -> b t c')

        # * Still need positional embedding, within the tokens in the sequence
        # I should already have 192 channels, from 16... in vision mamba, they use 3 convolutions until they reach that number
        # x = x + self.pos_embed # i will add positional embedding in the beginning

        # ! Dropout
        x = self.pos_drop(x)

        # mamba impl
        residual = None
        hidden_states = x
        for idx, layer in enumerate(self.layers):
            # ! This might be useful if we have to go through pretraining (i.e. if we run the model first on some other data)
            if self.use_checkpoint and idx < self.checkpoint_num:
                hidden_states, residual = layer(
                    hidden_states,
                    residual,
                    inference_params=inference_params,
                    use_checkpoint=True,
                )
            # ! hidden state and residual are other parts of the state space model.s
            else:
                hidden_states, residual = layer(
                    hidden_states, residual, inference_params=inference_params
                )

        if not self.fused_add_norm:
            if residual is None:
                residual = hidden_states
            else:
                residual = residual + self.drop_path(hidden_states)
            hidden_states = self.norm_f(residual.to(dtype=self.norm_f.weight.dtype))
        else:
            # Set prenorm=False here since we don't need the residual
            fused_add_norm_fn = (
                rms_norm_fn if isinstance(self.norm_f, RMSNorm) else layer_norm_fn
            )
            hidden_states = fused_add_norm_fn(
                self.drop_path(hidden_states),
                self.norm_f.weight,
                self.norm_f.bias,
                eps=self.norm_f.eps,
                residual=residual,
                prenorm=False,
                residual_in_fp32=self.residual_in_fp32,
            )

        return hidden_states

    # !then you can run forward here, to change the features of the forward inference parameter
    def forward(self, x, inference_params=None):
        x = self.forward_features(x, inference_params)

        return x


def inflate_weight(weight_2d, time_dim, center=True):
    print(f"Init center: {center}")
    if center:
        weight_3d = torch.zeros(*weight_2d.shape)
        weight_3d = weight_3d.unsqueeze(2).repeat(1, 1, time_dim, 1, 1)
        middle_idx = time_dim // 2
        weight_3d[:, :, middle_idx, :, :] = weight_2d
    else:
        weight_3d = weight_2d.unsqueeze(2).repeat(1, 1, time_dim, 1, 1)
        weight_3d = weight_3d / time_dim
    return weight_3d


def load_state_dict(model, state_dict, center=True):
    state_dict_3d = model.state_dict()
    for k in state_dict.keys():
        if k in state_dict_3d.keys() and state_dict[k].shape != state_dict_3d[k].shape:
            if len(state_dict_3d[k].shape) <= 3:
                print(f"Ignore: {k}")
                continue
            print(f"Inflate: {k}, {state_dict[k].shape} => {state_dict_3d[k].shape}")
            time_dim = state_dict_3d[k].shape[2]
            state_dict[k] = inflate_weight(state_dict[k], time_dim, center=center)

    del state_dict["head.weight"]
    del state_dict["head.bias"]
    msg = model.load_state_dict(state_dict, strict=False)
    print(msg)


@register_model
def videomamba_tiny(pretrained=False, **kwargs):
    # i commented some of the arguments that will be passed by **kwargs
    model = VisionMamba(
        # patch_size=4,
        # embed_dim=192,
        # depth=24,
        depth=12,  # this is what causes the increase in mempry usage.
        rms_norm=True,
        residual_in_fp32=True,
        fused_add_norm=True,
        # num_frames=16,
        **kwargs,  # this will include the arguments that I passed into videomamba_tiny
    )
    model.default_cfg = _cfg()
    if pretrained:
        print("load pretrained weights")
        state_dict = torch.load(_MODELS["videomamba_t16_in1k"], map_location="cpu")
        load_state_dict(model, state_dict, center=True)
    return model


if __name__ == "__main__":
    import time
    from fvcore.nn import FlopCountAnalysis
    from fvcore.nn import flop_count_table
    import numpy as np

    seed = 4217
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    num_frames = 8
    img_size = 224

    # To evaluate GFLOPs, pleaset set `rms_norm=False` and `fused_add_norm=False`
    model = videomamba_middle(num_frames=num_frames).cuda()
    flops = FlopCountAnalysis(
        model, torch.rand(1, 3, num_frames, img_size, img_size).cuda()
    )
    s = time.time()
    print(flop_count_table(flops, max_depth=1))
    print(time.time() - s)
