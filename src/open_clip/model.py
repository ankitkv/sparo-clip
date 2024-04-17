""" CLIP Model

Adapted from https://github.com/openai/CLIP. Originally MIT License, Copyright (c) 2021 OpenAI.
"""
from dataclasses import dataclass
import logging
import math
from typing import Optional, Tuple, Union

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.checkpoint import checkpoint

from .hf_model import HFTextEncoder
from .modified_resnet import ModifiedResNet
from .timm_model import TimmModel
from .transformer import LayerNormFp32, LayerNorm, QuickGELU, Attention, VisionTransformer, TextTransformer, SPAROVisionTransformer, SPAROTextTransformer, FDTQueryModel
from .utils import to_2tuple


@dataclass
class CLIPVisionCfg:
    layers: Union[Tuple[int, int, int, int], int] = 12
    width: int = 768
    head_width: int = 64
    mlp_ratio: float = 4.0
    patch_size: int = 16
    image_size: Union[Tuple[int, int], int] = 224

    ls_init_value: Optional[float] = None  # layer scale initial value
    patch_dropout: float = 0.  # what fraction of patches to dropout during training (0 would mean disabled and no patches dropped) - 0.5 to 0.75 recommended in the paper for optimal results
    input_patchnorm: bool = False  # whether to use dual patchnorm - would only apply the input layernorm on each patch, as post-layernorm already exist in original clip vit design
    global_average_pool: bool = False  # whether to global average pool the last embedding layer, instead of using CLS token (https://arxiv.org/abs/2205.01580)
    attentional_pool: bool = False  # whether to use attentional pooler in the last embedding layer
    n_queries: int = 256  # n_queries for attentional pooler
    attn_pooler_heads: int = 8  # n heads for attentional_pooling
    output_tokens: bool = False

    timm_model_name: str = None  # a valid model name overrides layers, width, patch_size
    timm_model_pretrained: bool = False  # use (imagenet) pretrained weights for named model
    timm_pool: str = 'avg'  # feature pooling for timm model ('abs_attn', 'rot_attn', 'avg', '')
    timm_proj: str = 'linear'  # linear projection for timm model output ('linear', 'mlp', '')
    timm_proj_bias: bool = False  # enable bias final projection
    timm_drop: float = 0.  # head dropout
    timm_drop_path: Optional[float] = None  # backbone stochastic depth


@dataclass
class CLIPTextCfg:
    context_length: int = 77
    vocab_size: int = 49408
    width: int = 512
    heads: int = 8
    layers: int = 12
    ls_init_value: Optional[float] = None  # layer scale initial value
    hf_model_name: str = None
    hf_tokenizer_name: str = None
    hf_model_pretrained: bool = True
    proj: str = 'mlp'
    pooler_type: str = 'mean_pooler'
    embed_cls: bool = False
    pad_id: int = 0
    output_tokens: bool = False
    global_average_pool: bool = False  # whether to global average pool the last embedding layer, instead of using EOS token
    attentional_pool: bool = False  # whether to use attentional pooler in the last embedding layer
    n_queries: int = 256  # n_queries for attentional pooler
    attn_pooler_heads: int = 8  # n heads for attentional_pooling


def get_cast_dtype(precision: str):
    cast_dtype = None
    if precision == 'bf16':
        cast_dtype = torch.bfloat16
    elif precision == 'fp16':
        cast_dtype = torch.float16
    return cast_dtype


def get_input_dtype(precision: str):
    input_dtype = None
    if precision in ('bf16', 'pure_bf16'):
        input_dtype = torch.bfloat16
    elif precision in ('fp16', 'pure_fp16'):
        input_dtype = torch.float16
    return input_dtype


def _build_vision_tower(
        embed_dim: int,
        vision_cfg: CLIPVisionCfg,
        quick_gelu: bool = False,
        cast_dtype: Optional[torch.dtype] = None,
        use_sparo: bool = False,
        L: Optional[int] = None,
        V: Optional[int] = None,
        sparo_attn_dim: Optional[int] = None,
        sparo_value_dim: Optional[int] = None,
        sparo_heads: Optional[int] = None,
        reduce_depth: Optional[int] = 0,
        share_kv: bool = True,
        use_codebook: bool = False,
):
    if isinstance(vision_cfg, dict):
        vision_cfg = CLIPVisionCfg(**vision_cfg)

    # OpenAI models are pretrained w/ QuickGELU but native nn.GELU is both faster and more
    # memory efficient in recent PyTorch releases (>= 1.10).
    # NOTE: timm models always use native GELU regardless of quick_gelu flag.
    act_layer = QuickGELU if quick_gelu else nn.GELU

    if vision_cfg.timm_model_name:
        visual = TimmModel(
            vision_cfg.timm_model_name,
            pretrained=vision_cfg.timm_model_pretrained,
            pool=vision_cfg.timm_pool,
            proj=vision_cfg.timm_proj,
            proj_bias=vision_cfg.timm_proj_bias,
            drop=vision_cfg.timm_drop,
            drop_path=vision_cfg.timm_drop_path,
            patch_drop=vision_cfg.patch_dropout if vision_cfg.patch_dropout > 0 else None,
            embed_dim=embed_dim,
            image_size=vision_cfg.image_size,
        )
    elif isinstance(vision_cfg.layers, (tuple, list)):
        assert not use_sparo
        vision_heads = vision_cfg.width * 32 // vision_cfg.head_width
        visual = ModifiedResNet(
            layers=(vision_cfg.layers[0], vision_cfg.layers[1], vision_cfg.layers[2] - reduce_depth, vision_cfg.layers[3]),
            output_dim=embed_dim,
            heads=vision_heads,
            image_size=vision_cfg.image_size,
            width=vision_cfg.width,
        )
    else:
        vision_heads = vision_cfg.width // vision_cfg.head_width
        norm_layer = LayerNormFp32 if cast_dtype in (torch.float16, torch.bfloat16) else LayerNorm
        if use_sparo:
            visual = SPAROVisionTransformer(
                image_size=vision_cfg.image_size,
                patch_size=vision_cfg.patch_size,
                width=vision_cfg.width,
                layers=vision_cfg.layers - reduce_depth,
                heads=vision_heads,
                mlp_ratio=vision_cfg.mlp_ratio,
                ls_init_value=vision_cfg.ls_init_value,
                patch_dropout=vision_cfg.patch_dropout,
                input_patchnorm=vision_cfg.input_patchnorm,
                global_average_pool=vision_cfg.global_average_pool,
                attentional_pool=vision_cfg.attentional_pool,
                n_queries=vision_cfg.n_queries,
                attn_pooler_heads=vision_cfg.attn_pooler_heads,
                output_tokens=vision_cfg.output_tokens,
                output_dim=embed_dim,
                act_layer=act_layer,
                norm_layer=norm_layer,
                L=L,
                V=V,
                attn_dim=sparo_attn_dim,
                value_dim=sparo_value_dim,
                sparo_heads=sparo_heads,
                share_kv=share_kv,
            )
        else:
            visual = VisionTransformer(
                image_size=vision_cfg.image_size,
                patch_size=vision_cfg.patch_size,
                width=vision_cfg.width,
                layers=vision_cfg.layers - reduce_depth,
                heads=vision_heads,
                mlp_ratio=vision_cfg.mlp_ratio,
                ls_init_value=vision_cfg.ls_init_value,
                patch_dropout=vision_cfg.patch_dropout,
                input_patchnorm=vision_cfg.input_patchnorm,
                global_average_pool=vision_cfg.global_average_pool,
                attentional_pool=vision_cfg.attentional_pool,
                n_queries=vision_cfg.n_queries,
                attn_pooler_heads=vision_cfg.attn_pooler_heads,
                output_tokens=vision_cfg.output_tokens,
                output_dim=embed_dim,
                act_layer=act_layer,
                norm_layer=norm_layer,
                use_codebook=use_codebook,
            )

    return visual


def _build_text_tower(
        embed_dim: int,
        text_cfg: CLIPTextCfg,
        quick_gelu: bool = False,
        cast_dtype: Optional[torch.dtype] = None,
        use_sparo: bool = False,
        L: Optional[int] = None,
        V: Optional[int] = None,
        sparo_attn_dim: Optional[int] = None,
        sparo_value_dim: Optional[int] = None,
        sparo_heads: Optional[int] = None,
        reduce_depth: Optional[int] = 0,
        share_kv: bool = True,
        use_codebook: bool = False,
):
    if isinstance(text_cfg, dict):
        text_cfg = CLIPTextCfg(**text_cfg)

    if text_cfg.hf_model_name:
        text = HFTextEncoder(
            text_cfg.hf_model_name,
            output_dim=embed_dim,
            proj=text_cfg.proj,
            pooler_type=text_cfg.pooler_type,
            pretrained=text_cfg.hf_model_pretrained,
            output_tokens=text_cfg.output_tokens,
        )
    else:
        act_layer = QuickGELU if quick_gelu else nn.GELU
        norm_layer = LayerNormFp32 if cast_dtype in (torch.float16, torch.bfloat16) else LayerNorm

        if use_sparo:
            text = SPAROTextTransformer(
                context_length=text_cfg.context_length,
                vocab_size=text_cfg.vocab_size,
                width=text_cfg.width,
                heads=text_cfg.heads,
                layers=text_cfg.layers - reduce_depth,
                ls_init_value=text_cfg.ls_init_value,
                output_dim=embed_dim,
                embed_cls=text_cfg.embed_cls,
                output_tokens=text_cfg.output_tokens,
                pad_id=text_cfg.pad_id,
                act_layer=act_layer,
                norm_layer=norm_layer,
                global_average_pool=text_cfg.global_average_pool,
                attentional_pool=text_cfg.attentional_pool,
                n_queries=text_cfg.n_queries,
                attn_pooler_heads=text_cfg.attn_pooler_heads,
                L=L,
                V=V,
                attn_dim=sparo_attn_dim,
                value_dim=sparo_value_dim,
                sparo_heads=sparo_heads,
                share_kv=share_kv,
            )
        else:
            text = TextTransformer(
                context_length=text_cfg.context_length,
                vocab_size=text_cfg.vocab_size,
                width=text_cfg.width,
                heads=text_cfg.heads,
                layers=text_cfg.layers - reduce_depth,
                ls_init_value=text_cfg.ls_init_value,
                output_dim=embed_dim,
                embed_cls=text_cfg.embed_cls,
                output_tokens=text_cfg.output_tokens,
                pad_id=text_cfg.pad_id,
                act_layer=act_layer,
                norm_layer=norm_layer,
                global_average_pool=text_cfg.global_average_pool,
                attentional_pool=text_cfg.attentional_pool,
                n_queries=text_cfg.n_queries,
                attn_pooler_heads=text_cfg.attn_pooler_heads,
                use_codebook=use_codebook,
            )
    return text


class CLIP(nn.Module):
    output_dict: torch.jit.Final[bool]

    def __init__(
            self,
            embed_dim: int,
            vision_cfg: CLIPVisionCfg,
            text_cfg: CLIPTextCfg,
            quick_gelu: bool = False,
            cast_dtype: Optional[torch.dtype] = None,
            output_dict: bool = False,
            use_sparo: bool = False,
            sparo_attn_dim: Optional[int] = None,
            sparo_value_dim: Optional[int] = None,
            sparo_heads: Optional[int] = None,
            L: Optional[int] = None,
            V: Optional[int] = None,
            reduce_depth: Optional[int] = 0,
            sparo_type: str = "cont:const",
            share_kv: bool = True,
            share_queries: Optional[bool] = False,
            use_codebook: bool = False,
            num_codes: int = 16384,
            bottleneck_dim: Optional[int] = None,
    ):
        super().__init__()
        self.embed_dim = embed_dim
        self.output_dict = output_dict
        self.use_sparo = use_sparo
        self.L = L
        self.V = V
        self.sparo_type = sparo_type
        self.use_codebook = use_codebook
        self.bottleneck_dim = bottleneck_dim

        if use_sparo and use_codebook:
            raise ValueError("SPARO and codebook cannot be used together yet.")

        if not use_sparo or not sparo_type.endswith("softmax"):
            self.model_V = V
        else:
            self.model_V = V + 1

        self.visual = _build_vision_tower(
            embed_dim=bottleneck_dim or embed_dim,
            vision_cfg=vision_cfg,
            quick_gelu=quick_gelu,
            cast_dtype=cast_dtype,
            use_sparo=use_sparo,
            L=L,
            V=self.model_V,
            sparo_attn_dim=sparo_attn_dim,
            sparo_value_dim=sparo_value_dim,
            sparo_heads=sparo_heads,
            reduce_depth=reduce_depth,
            share_kv=share_kv,
            use_codebook=use_codebook,
        )
        text = _build_text_tower(
            embed_dim=bottleneck_dim or embed_dim,
            text_cfg=text_cfg,
            quick_gelu=quick_gelu,
            cast_dtype=cast_dtype,
            use_sparo=use_sparo,
            L=L,
            V=self.model_V,
            sparo_attn_dim=sparo_attn_dim,
            sparo_value_dim=sparo_value_dim,
            sparo_heads=sparo_heads,
            reduce_depth=reduce_depth,
            share_kv=share_kv,
            use_codebook=use_codebook,
        )
        if self.use_sparo and share_queries:
            text.sparo.q_emb = self.visual.sparo.q_emb

        if bottleneck_dim is not None:
            self.vision_bottleproj = nn.Linear(bottleneck_dim, embed_dim)
            self.text_bottleproj = nn.Linear(bottleneck_dim, embed_dim)

        self.transformer = text.transformer
        self.context_length = text.context_length
        self.vocab_size = text.vocab_size
        self.token_embedding = text.token_embedding
        self.positional_embedding = text.positional_embedding
        if not use_codebook:
            self.text_attn_pool = text.attn_pool
            self.text_global_average_pool = text.global_average_pool
            self.ln_final = text.ln_final
            if not use_sparo:
                self.text_projection = text.text_projection
            else:
                self.text_sparo = text.sparo

        if use_codebook:
            #learnable FDT
            self.space_dict = nn.Parameter(torch.randn(num_codes, bottleneck_dim or embed_dim))

            #query mapping
            self.img_query_model = FDTQueryModel(self.visual.width, bottleneck_dim or embed_dim)
            self.txt_query_model = FDTQueryModel(text.width, bottleneck_dim or embed_dim)

        self.register_buffer('attn_mask', text.attn_mask, persistent=False)

        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))

    def lock_image_tower(self, unlocked_groups=0, freeze_bn_stats=False):
        # lock image tower as per LiT - https://arxiv.org/abs/2111.07991
        self.visual.lock(unlocked_groups=unlocked_groups, freeze_bn_stats=freeze_bn_stats)

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.visual.set_grad_checkpointing(enable)
        self.transformer.grad_checkpointing = enable

    def _project_for_sparo(self, x):
        x = x.view(*x.shape[:-1], self.L, self.model_V)

        rep_type, norm_type = self.sparo_type.split(":")
        if norm_type == "sqrtsoftmax":
            weights = torch.sqrt(F.softmax(x[..., :1], dim=-2))
            x = x[..., 1:]
        elif norm_type == "softmax":
            weights = F.normalize(F.softmax(x[..., :1], dim=-2), dim=-2)
            x = x[..., 1:]
        elif norm_type == "norm":
            if rep_type == "cont":
                norm_in = x
            elif rep_type == "sqrtsem":
                norm_in = torch.exp(x / 2.0)
            else:
                raise NotImplementedError
            full_norm = torch.norm(norm_in.view(*norm_in.shape[:-2], -1), dim=-1, keepdim=True).unsqueeze(-1)
            weights = torch.norm(norm_in, dim=-1, keepdim=True) / full_norm
        elif norm_type == "const":
            weights = torch.sqrt(torch.ones_like(x[..., :1]) / self.L)
        else:
            raise NotImplementedError
        if rep_type == "cont":
            out_rep = F.normalize(x, dim=-1)
        elif rep_type == "sqrtsem":
            out_rep = torch.sqrt(F.softmax(x, dim=-1))
        elif rep_type == "sem":
            out_rep = F.normalize(F.softmax(x, dim=-1), dim=-1)
        else:
            raise NotImplementedError
        out = out_rep * weights
        return out

    def encode_image(self, image, normalize: bool = False, return_sparo: bool = False, return_attn=False):
        features = self.visual(image)
        if not self.use_sparo:
            if self.use_codebook:
                features = self.img_query_model(features, self.space_dict)
            if self.bottleneck_dim is not None:
                features = self.vision_bottleproj(features)
            return F.normalize(features, dim=-1) if normalize else features
        else:
            assert normalize
            out, attn = features
            out = self._project_for_sparo(out)
            if return_sparo:
                if return_attn:
                    return out, attn
                else:
                    return out
            else:
                image_features = out.view(*out.shape[:-2], -1)
                return image_features

    def encode_text(self, text, normalize: bool = False, return_sparo: bool = False, return_attn=False):
        cast_dtype = self.transformer.get_cast_dtype()

        x = self.token_embedding(text).to(cast_dtype)  # [batch_size, n_ctx, d_model]

        x = x + self.positional_embedding.to(cast_dtype)
        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x, attn_mask=self.attn_mask)
        x = x.permute(1, 0, 2)  # LND -> NLD
        if not self.use_codebook:
            if self.text_attn_pool is not None:
                x = self.text_attn_pool(x, text.argmax(dim=-1))
            x = self.ln_final(x)  # [batch_size, n_ctx, transformer.width]

        if not self.use_sparo:
            if not self.use_codebook:
                if self.text_attn_pool is not None:
                    if self.text_global_average_pool:
                        x = x.mean(dim=1)
                    else:
                        x = x[:, 0]
                else:
                    if self.text_global_average_pool:
                        raise NotImplementedError  # TODO
                    else:
                        # take features from the eot embedding (eot_token is the highest number in each sequence)
                        x = x[torch.arange(x.shape[0]), text.argmax(dim=-1)]
                x = x @ self.text_projection
            else:
                x = self.txt_query_model(x, self.space_dict, eos_pos=text.argmax(dim=-1))
            if self.bottleneck_dim is not None:
                x = self.text_bottleproj(x)
            return F.normalize(x, dim=-1) if normalize else x
        else:
            assert normalize
            # take features from the eot embedding (eot_token is the highest number in each sequence)
            out, attn = self.text_sparo(x, text.argmax(dim=-1))
            out = out.view(-1, self.L * self.model_V)
            out = self._project_for_sparo(out)
            if return_sparo:
                if return_attn:
                    return out, attn
                else:
                    return out
            else:
                text_features = out.view(*out.shape[:-2], -1)
                return text_features

    def forward(
            self,
            image: Optional[torch.Tensor] = None,
            text: Optional[torch.Tensor] = None,
    ):
        image_features = self.encode_image(image, normalize=True) if image is not None else None
        text_features = self.encode_text(text, normalize=True) if text is not None else None
        if self.output_dict:
            return {
                "image_features": image_features,
                "text_features": text_features,
                "logit_scale": self.logit_scale.exp()
            }
        return image_features, text_features, self.logit_scale.exp()


class CustomTextCLIP(nn.Module):
    output_dict: torch.jit.Final[bool]

    def __init__(
            self,
            embed_dim: int,
            vision_cfg: CLIPVisionCfg,
            text_cfg: CLIPTextCfg,
            quick_gelu: bool = False,
            cast_dtype: Optional[torch.dtype] = None,
            output_dict: bool = False,
    ):
        super().__init__()
        self.output_dict = output_dict
        self.visual = _build_vision_tower(embed_dim, vision_cfg, quick_gelu, cast_dtype)
        self.text = _build_text_tower(embed_dim, text_cfg, quick_gelu, cast_dtype)
        self.context_length = self.text.context_length
        self.vocab_size = self.text.vocab_size
        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))

    def lock_image_tower(self, unlocked_groups=0, freeze_bn_stats=False):
        # lock image tower as per LiT - https://arxiv.org/abs/2111.07991
        self.visual.lock(unlocked_groups=unlocked_groups, freeze_bn_stats=freeze_bn_stats)

    def lock_text_tower(self, unlocked_layers: int = 0, freeze_layer_norm: bool = True):
        self.text.lock(unlocked_layers, freeze_layer_norm)

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.visual.set_grad_checkpointing(enable)
        self.text.set_grad_checkpointing(enable)

    def encode_image(self, image, normalize: bool = False):
        features = self.visual(image)
        return F.normalize(features, dim=-1) if normalize else features

    def encode_text(self, text, normalize: bool = False):
        features = self.text(text)
        return F.normalize(features, dim=-1) if normalize else features

    def forward(
            self,
            image: Optional[torch.Tensor] = None,
            text: Optional[torch.Tensor] = None,
    ):
        image_features = self.encode_image(image, normalize=True) if image is not None else None
        text_features = self.encode_text(text, normalize=True) if text is not None else None
        if self.output_dict:
            return {
                "image_features": image_features,
                "text_features": text_features,
                "logit_scale": self.logit_scale.exp()
            }
        return image_features, text_features, self.logit_scale.exp()


def convert_weights_to_lp(model: nn.Module, dtype=torch.float16):
    """Convert applicable model parameters to low-precision (bf16 or fp16)"""

    def _convert_weights(l):
        if isinstance(l, (nn.Conv1d, nn.Conv2d, nn.Linear)):
            l.weight.data = l.weight.data.to(dtype)
            if l.bias is not None:
                l.bias.data = l.bias.data.to(dtype)

        if isinstance(l, (nn.MultiheadAttention, Attention)):
            for attr in [*[f"{s}_proj_weight" for s in ["in", "q", "k", "v"]], "in_proj_bias", "bias_k", "bias_v"]:
                tensor = getattr(l, attr)
                if tensor is not None:
                    tensor.data = tensor.data.to(dtype)

        if isinstance(l, (CLIP, TextTransformer)):
            # convert text nn.Parameter projections
            attr = getattr(l, "text_projection", None)
            if attr is not None:
                attr.data = attr.data.to(dtype)

        if isinstance(l, VisionTransformer):
            # convert vision nn.Parameter projections
            attr = getattr(l, "proj", None)
            if attr is not None:
                attr.data = attr.data.to(dtype)

    model.apply(_convert_weights)


convert_weights_to_fp16 = convert_weights_to_lp  # backwards compat


# used to maintain checkpoint compatibility
def convert_to_custom_text_state_dict(state_dict: dict):
    if 'text_projection' in state_dict:
        # old format state_dict, move text tower -> .text
        new_state_dict = {}
        for k, v in state_dict.items():
            if any(k.startswith(p) for p in (
                'text_projection',
                'positional_embedding',
                'token_embedding',
                'transformer',
                'ln_final',
            )):
                k = 'text.' + k
            new_state_dict[k] = v
        return new_state_dict
    return state_dict


def build_model_from_openai_state_dict(
        state_dict: dict,
        quick_gelu=True,
        cast_dtype=torch.float16,
):
    vit = "visual.proj" in state_dict

    if vit:
        vision_width = state_dict["visual.conv1.weight"].shape[0]
        vision_layers = len(
            [k for k in state_dict.keys() if k.startswith("visual.") and k.endswith(".attn.in_proj_weight")])
        vision_patch_size = state_dict["visual.conv1.weight"].shape[-1]
        grid_size = round((state_dict["visual.positional_embedding"].shape[0] - 1) ** 0.5)
        image_size = vision_patch_size * grid_size
    else:
        counts: list = [
            len(set(k.split(".")[2] for k in state_dict if k.startswith(f"visual.layer{b}"))) for b in [1, 2, 3, 4]]
        vision_layers = tuple(counts)
        vision_width = state_dict["visual.layer1.0.conv1.weight"].shape[0]
        output_width = round((state_dict["visual.attnpool.positional_embedding"].shape[0] - 1) ** 0.5)
        vision_patch_size = None
        assert output_width ** 2 + 1 == state_dict["visual.attnpool.positional_embedding"].shape[0]
        image_size = output_width * 32

    embed_dim = state_dict["text_projection"].shape[1]
    context_length = state_dict["positional_embedding"].shape[0]
    vocab_size = state_dict["token_embedding.weight"].shape[0]
    transformer_width = state_dict["ln_final.weight"].shape[0]
    transformer_heads = transformer_width // 64
    transformer_layers = len(set(k.split(".")[2] for k in state_dict if k.startswith(f"transformer.resblocks")))

    vision_cfg = CLIPVisionCfg(
        layers=vision_layers,
        width=vision_width,
        patch_size=vision_patch_size,
        image_size=image_size,
    )
    text_cfg = CLIPTextCfg(
        context_length=context_length,
        vocab_size=vocab_size,
        width=transformer_width,
        heads=transformer_heads,
        layers=transformer_layers,
    )
    model = CLIP(
        embed_dim,
        vision_cfg=vision_cfg,
        text_cfg=text_cfg,
        quick_gelu=quick_gelu,  # OpenAI models were trained with QuickGELU
        cast_dtype=cast_dtype,
    )

    for key in ["input_resolution", "context_length", "vocab_size"]:
        state_dict.pop(key, None)

    convert_weights_to_fp16(model)  # OpenAI state dicts are partially converted to float16
    model.load_state_dict(state_dict)
    return model.eval()


def trace_model(model, batch_size=256, device=torch.device('cpu')):
    model.eval()
    image_size = model.visual.image_size
    example_images = torch.ones((batch_size, 3, image_size, image_size), device=device)
    example_text = torch.zeros((batch_size, model.context_length), dtype=torch.int, device=device)
    model = torch.jit.trace_module(
        model,
        inputs=dict(
            forward=(example_images, example_text),
            encode_text=(example_text,),
            encode_image=(example_images,)
        ))
    model.visual.image_size = image_size
    return model


def resize_pos_embed(state_dict, model, interpolation: str = 'bicubic', antialias: bool = True):
    # Rescale the grid of position embeddings when loading from state_dict
    old_pos_embed = state_dict.get('visual.positional_embedding', None)
    if old_pos_embed is None or not hasattr(model.visual, 'grid_size'):
        return
    grid_size = to_2tuple(model.visual.grid_size)
    extra_tokens = 1  # FIXME detect different token configs (ie no class token, or more)
    new_seq_len = grid_size[0] * grid_size[1] + extra_tokens
    if new_seq_len == old_pos_embed.shape[0]:
        return

    if extra_tokens:
        pos_emb_tok, pos_emb_img = old_pos_embed[:extra_tokens], old_pos_embed[extra_tokens:]
    else:
        pos_emb_tok, pos_emb_img = None, old_pos_embed
    old_grid_size = to_2tuple(int(math.sqrt(len(pos_emb_img))))

    logging.info('Resizing position embedding grid-size from %s to %s', old_grid_size, grid_size)
    pos_emb_img = pos_emb_img.reshape(1, old_grid_size[0], old_grid_size[1], -1).permute(0, 3, 1, 2)
    pos_emb_img = F.interpolate(
        pos_emb_img,
        size=grid_size,
        mode=interpolation,
        antialias=antialias,
        align_corners=False,
    )
    pos_emb_img = pos_emb_img.permute(0, 2, 3, 1).reshape(1, grid_size[0] * grid_size[1], -1)[0]
    if pos_emb_tok is not None:
        new_pos_embed = torch.cat([pos_emb_tok, pos_emb_img], dim=0)
    else:
        new_pos_embed = pos_emb_img
    state_dict['visual.positional_embedding'] = new_pos_embed
