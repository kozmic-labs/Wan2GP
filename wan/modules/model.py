# Copyright 2024-2025 The Alibaba Wan Team Authors. All rights reserved.
import math

import torch
import torch.cuda.amp as amp
import torch.nn as nn
from diffusers.configuration_utils import ConfigMixin, register_to_config
from diffusers.models.modeling_utils import ModelMixin
import numpy as np
from typing import Union,Optional
from mmgp import offload
from .attention import pay_attention

__all__ = ['WanModel']


def sinusoidal_embedding_1d(dim, position):
    # preprocess
    assert dim % 2 == 0
    half = dim // 2
    position = position.type(torch.float32)

    # calculation
    sinusoid = torch.outer(
        position, torch.pow(10000, -torch.arange(half).to(position).div(half)))
    x = torch.cat([torch.cos(sinusoid), torch.sin(sinusoid)], dim=1)
    return x




def identify_k( b: float, d: int, N: int):
    """
    This function identifies the index of the intrinsic frequency component in a RoPE-based pre-trained diffusion transformer.

    Args:
        b (`float`): The base frequency for RoPE.
        d (`int`): Dimension of the frequency tensor
        N (`int`): the first observed repetition frame in latent space
    Returns:
        k (`int`): the index of intrinsic frequency component
        N_k (`int`): the period of intrinsic frequency component in latent space
    Example:
        In HunyuanVideo, b=256 and d=16, the repetition occurs approximately 8s (N=48 in latent space).
        k, N_k = identify_k(b=256, d=16, N=48)
        In this case, the intrinsic frequency index k is 4, and the period N_k is 50.
    """

    # Compute the period of each frequency in RoPE according to Eq.(4)
    periods = []
    for j in range(1, d // 2 + 1):
        theta_j = 1.0 / (b ** (2 * (j - 1) / d))
        N_j = round(2 * torch.pi / theta_j)
        periods.append(N_j)

    # Identify the intrinsic frequency whose period is closed to N（see Eq.(7)）
    diffs = [abs(N_j - N) for N_j in periods]
    k = diffs.index(min(diffs)) + 1
    N_k = periods[k-1]
    return k, N_k

def rope_params_riflex(max_seq_len, dim, theta=10000, L_test=30, k=6):
    assert dim % 2 == 0
    exponents = torch.arange(0, dim, 2, dtype=torch.float64).div(dim)
    inv_theta_pow = 1.0 / torch.pow(theta, exponents)
    
    inv_theta_pow[k-1] = 0.9 * 2 * torch.pi / L_test
        
    freqs = torch.outer(torch.arange(max_seq_len), inv_theta_pow)
    if True:
        freqs_cos = freqs.cos().repeat_interleave(2, dim=1).float()  # [S, D]
        freqs_sin = freqs.sin().repeat_interleave(2, dim=1).float()  # [S, D]
        return (freqs_cos, freqs_sin)
    else:
        freqs = torch.polar(torch.ones_like(freqs), freqs)  # complex64     # [S, D/2]
    return freqs




def rope_apply_(x, grid_sizes, freqs):
    assert x.shape[0]==1

    n, c = x.size(2), x.size(3) // 2

    # split freqs
    freqs = freqs.split([c - 2 * (c // 3), c // 3, c // 3], dim=1)

    f, h, w = grid_sizes[0]
    seq_len = f * h * w
    x_i = x[0, :seq_len, :, :]

    x_i = x_i.to(torch.float32)
    x_i = x_i.reshape(seq_len, n, -1, 2)        
    x_i = torch.view_as_complex(x_i)
    freqs_i = torch.cat([
        freqs[0][:f].view(f, 1, 1, -1).expand(f, h, w, -1),
        freqs[1][:h].view(1, h, 1, -1).expand(f, h, w, -1),
        freqs[2][:w].view(1, 1, w, -1).expand(f, h, w, -1)
    ], dim=-1)
    freqs_i= freqs_i.reshape(seq_len, 1, -1)

    # apply rotary embedding
    x_i *= freqs_i
    x_i = torch.view_as_real(x_i).flatten(2)
    x[0, :seq_len, :, :] = x_i.to(torch.bfloat16)
    # x_i = torch.cat([x_i, x[0, seq_len:]])
    return x

# @amp.autocast(enabled=False)
def rope_apply(x, grid_sizes, freqs):
    n, c = x.size(2), x.size(3) // 2

    # split freqs
    freqs = freqs.split([c - 2 * (c // 3), c // 3, c // 3], dim=1)

    # loop over samples
    output = []
    for i, (f, h, w) in enumerate(grid_sizes):
        seq_len = f * h * w

        # precompute multipliers
        # x_i = x[i, :seq_len]
        x_i = x[i]
        x_i = x_i[:seq_len, :, :]

        x_i = x_i.to(torch.float32)
        x_i = x_i.reshape(seq_len, n, -1, 2)        
        x_i = torch.view_as_complex(x_i)
        freqs_i = torch.cat([
            freqs[0][:f].view(f, 1, 1, -1).expand(f, h, w, -1),
            freqs[1][:h].view(1, h, 1, -1).expand(f, h, w, -1),
            freqs[2][:w].view(1, 1, w, -1).expand(f, h, w, -1)
        ],
                            dim=-1).reshape(seq_len, 1, -1)

        # apply rotary embedding
        x_i *= freqs_i
        x_i = torch.view_as_real(x_i).flatten(2)
        x_i = x_i.to(torch.bfloat16)
        x_i = torch.cat([x_i, x[i, seq_len:]])

        # append to collection
        output.append(x_i)
    return torch.stack(output) #.float()

def relative_l1_distance(last_tensor, current_tensor):
    l1_distance = torch.abs(last_tensor - current_tensor).mean()
    norm = torch.abs(last_tensor).mean()
    relative_l1_distance = l1_distance / norm
    return relative_l1_distance.to(torch.float32)

class WanRMSNorm(nn.Module):

    def __init__(self, dim, eps=1e-5):
        super().__init__()
        self.dim = dim
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        r"""
        Args:
            x(Tensor): Shape [B, L, C]
        """
        y = x.float()
        y.pow_(2)
        y = y.mean(dim=-1, keepdim=True)
        y += self.eps
        y.rsqrt_()
        x *=  y
        x *= self.weight
        return x
        # return self._norm(x).type_as(x) * self.weight

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(dim=-1, keepdim=True) + self.eps)

def my_LayerNorm(norm, x):
        y = x.float()
        y_m = y.mean(dim=-1, keepdim=True)
        y -= y_m 
        del y_m
        y.pow_(2)
        y = y.mean(dim=-1, keepdim=True)
        y += norm.eps
        y.rsqrt_()
        x = x *  y
        return x


class WanLayerNorm(nn.LayerNorm):

    def __init__(self, dim, eps=1e-6, elementwise_affine=False):
        super().__init__(dim, elementwise_affine=elementwise_affine, eps=eps)

    def forward(self, x):
        r"""
        Args:
            x(Tensor): Shape [B, L, C]
        """
        # return F.layer_norm(
        #     input, self.normalized_shape, self.weight, self.bias, self.eps
        # )
        y = super().forward(x)
        x = y.type_as(x)
        return x
        # return super().forward(x).type_as(x)

from wan.modules.posemb_layers import apply_rotary_emb

class WanSelfAttention(nn.Module):

    def __init__(self,
                 dim,
                 num_heads,
                 window_size=(-1, -1),
                 qk_norm=True,
                 eps=1e-6):
        assert dim % num_heads == 0
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.window_size = window_size
        self.qk_norm = qk_norm
        self.eps = eps

        # layers
        self.q = nn.Linear(dim, dim)
        self.k = nn.Linear(dim, dim)
        self.v = nn.Linear(dim, dim)
        self.o = nn.Linear(dim, dim)
        self.norm_q = WanRMSNorm(dim, eps=eps) if qk_norm else nn.Identity()
        self.norm_k = WanRMSNorm(dim, eps=eps) if qk_norm else nn.Identity()

    def forward(self, xlist, seq_lens, grid_sizes, freqs):
        r"""
        Args:
            x(Tensor): Shape [B, L, num_heads, C / num_heads]
            seq_lens(Tensor): Shape [B]
            grid_sizes(Tensor): Shape [B, 3], the second dimension contains (F, H, W)
            freqs(Tensor): Rope freqs, shape [1024, C / num_heads / 2]
        """
        x = xlist[0]
        xlist.clear()

        b, s, n, d = *x.shape[:2], self.num_heads, self.head_dim

        # query, key, value function
        q = self.q(x)
        self.norm_q(q)
        q = q.view(b, s, n, d) # !!!
        k = self.k(x)
        self.norm_k(k)
        k = k.view(b, s, n, d) 
        v = self.v(x).view(b, s, n, d)
        del x
        # rope_apply_(q, grid_sizes, freqs)
        # rope_apply_(k, grid_sizes, freqs)
        qklist = [q,k]
        del q,k
        q,k = apply_rotary_emb(qklist, freqs, head_first=False)
        qkv_list = [q,k,v]
        del q,k,v
        x = pay_attention(
            qkv_list,
            # q=q,
            # k=k,
            # v=v,
            # k_lens=seq_lens,
            window_size=self.window_size)
        # output
        x = x.flatten(2)
        x = self.o(x)
        return x


class WanT2VCrossAttention(WanSelfAttention):

    def forward(self, xlist, context, context_lens):
        r"""
        Args:
            x(Tensor): Shape [B, L1, C]
            context(Tensor): Shape [B, L2, C]
            context_lens(Tensor): Shape [B]
        """
        x = xlist[0]
        xlist.clear()
        b, n, d = x.size(0), self.num_heads, self.head_dim

        # compute query, key, value
        q = self.q(x)
        del x
        self.norm_q(q)
        q= q.view(b, -1, n, d)
        k = self.k(context)
        self.norm_k(k)
        k = k.view(b, -1, n, d)
        v = self.v(context).view(b, -1, n, d)

        # compute attention
        qvl_list=[q, k, v]
        del q, k, v
        x = pay_attention(qvl_list, k_lens=context_lens, cross_attn= True)

        # output
        x = x.flatten(2)
        x = self.o(x)
        return x


class WanI2VCrossAttention(WanSelfAttention):

    def __init__(self,
                 dim,
                 num_heads,
                 window_size=(-1, -1),
                 qk_norm=True,
                 eps=1e-6):
        super().__init__(dim, num_heads, window_size, qk_norm, eps)

        self.k_img = nn.Linear(dim, dim)
        self.v_img = nn.Linear(dim, dim)
        # self.alpha = nn.Parameter(torch.zeros((1, )))
        self.norm_k_img = WanRMSNorm(dim, eps=eps) if qk_norm else nn.Identity()

    def forward(self, xlist, context, context_lens):
        r"""
        Args:
            x(Tensor): Shape [B, L1, C]
            context(Tensor): Shape [B, L2, C]
            context_lens(Tensor): Shape [B]
        """

        ##### Enjoy this spagheti VRAM optimizations done by DeepBeepMeep !
        # I am sure you are a nice person and as you copy this code, you will give me officially proper credits:
        # Please link to https://github.com/deepbeepmeep/Wan2GP and @deepbeepmeep on twitter  

        x = xlist[0]
        xlist.clear()

        context_img = context[:, :257]
        context = context[:, 257:]
        b, n, d = x.size(0), self.num_heads, self.head_dim

        # compute query, key, value
        q = self.q(x)
        del x
        self.norm_q(q)
        q= q.view(b, -1, n, d)
        k = self.k(context)
        self.norm_k(k)
        k = k.view(b, -1, n, d)
        v = self.v(context).view(b, -1, n, d)

        qkv_list = [q, k, v]
        del k,v
        x = pay_attention(qkv_list, k_lens=context_lens)

        k_img = self.k_img(context_img)
        self.norm_k_img(k_img)
        k_img = k_img.view(b, -1, n, d)
        v_img = self.v_img(context_img).view(b, -1, n, d)
        qkv_list = [q, k_img, v_img]
        del q, k_img, v_img
        img_x = pay_attention(qkv_list, k_lens=None)
        # compute attention


        # output
        x = x.flatten(2)
        img_x = img_x.flatten(2)
        x += img_x
        del img_x
        x = self.o(x)
        return x



WAN_CROSSATTENTION_CLASSES = {
    't2v_cross_attn': WanT2VCrossAttention,
    'i2v_cross_attn': WanI2VCrossAttention,
}


class WanAttentionBlock(nn.Module):

    def __init__(self,
                 cross_attn_type,
                 dim,
                 ffn_dim,
                 num_heads,
                 window_size=(-1, -1),
                 qk_norm=True,
                 cross_attn_norm=False,
                 eps=1e-6,
                 block_id=None
                 ):
        super().__init__()
        self.dim = dim
        self.ffn_dim = ffn_dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.qk_norm = qk_norm
        self.cross_attn_norm = cross_attn_norm
        self.eps = eps

        # layers
        self.norm1 = WanLayerNorm(dim, eps)
        self.self_attn = WanSelfAttention(dim, num_heads, window_size, qk_norm,
                                          eps)
        self.norm3 = WanLayerNorm(
            dim, eps,
            elementwise_affine=True) if cross_attn_norm else nn.Identity()
        self.cross_attn = WAN_CROSSATTENTION_CLASSES[cross_attn_type](dim,
                                                                      num_heads,
                                                                      (-1, -1),
                                                                      qk_norm,
                                                                      eps)
        self.norm2 = WanLayerNorm(dim, eps)
        self.ffn = nn.Sequential(
            nn.Linear(dim, ffn_dim), nn.GELU(approximate='tanh'),
            nn.Linear(ffn_dim, dim))

        # modulation
        self.modulation = nn.Parameter(torch.randn(1, 6, dim) / dim**0.5)
        self.block_id = block_id

    def forward(
        self,
        x,
        e,
        seq_lens,
        grid_sizes,
        freqs,
        context,
        context_lens,
        hints= None, 
        context_scale=1.0,
    ):
        r"""
        Args:
            x(Tensor): Shape [B, L, C]
            e(Tensor): Shape [B, 6, C]
            seq_lens(Tensor): Shape [B], length of each sequence in batch
            grid_sizes(Tensor): Shape [B, 3], the second dimension contains (F, H, W)
            freqs(Tensor): Rope freqs, shape [1024, C / num_heads / 2]
        """
        hint = None
        if self.block_id is not None and hints is not None:
            kwargs = { 
                "seq_lens" : seq_lens,
                "grid_sizes" : grid_sizes,
                "freqs" :freqs, 
                "context" : context,
                "context_lens" : context_lens,
                "e" : e,
            }
            if self.block_id == 0:
                hint = self.vace(hints, x, **kwargs)
            else:
                hint = self.vace(hints, None, **kwargs)

        e = (self.modulation + e).chunk(6, dim=1)
 
        # self-attention
        x_mod = self.norm1(x)
        x_mod *= 1 + e[1]
        x_mod += e[0]
        xlist = [x_mod]
        del x_mod
        y = self.self_attn( xlist, seq_lens, grid_sizes,freqs)
        x.addcmul_(y, e[2])
        del y
        y = self.norm3(x)
        ylist= [y]
        del y
        x += self.cross_attn(ylist, context, context_lens)
        y = self.norm2(x)

        y *= 1 + e[4]
        y += e[3]


        ffn = self.ffn[0]
        gelu = self.ffn[1]
        ffn2= self.ffn[2]

        y_shape = y.shape
        y = y.view(-1, y_shape[-1])
        chunk_size = int(y_shape[1]/2.7)
        chunks =torch.split(y, chunk_size)
        for y_chunk  in chunks:
            mlp_chunk = ffn(y_chunk)
            mlp_chunk = gelu(mlp_chunk)
            y_chunk[...] = ffn2(mlp_chunk)
            del mlp_chunk 
        y = y.view(y_shape)

        x.addcmul_(y, e[5])



        if hint is not None:
            if context_scale == 1:
                x.add_(hint)
            else:
                x.add_(hint, alpha= context_scale)
        return x 



class VaceWanAttentionBlock(WanAttentionBlock):
    def __init__(
            self,
            cross_attn_type,
            dim,
            ffn_dim,
            num_heads,
            window_size=(-1, -1),
            qk_norm=True,
            cross_attn_norm=False,
            eps=1e-6,
            block_id=0
    ):
        super().__init__(cross_attn_type, dim, ffn_dim, num_heads, window_size, qk_norm, cross_attn_norm, eps)
        self.block_id = block_id
        if block_id == 0:
            self.before_proj = nn.Linear(self.dim, self.dim)
            nn.init.zeros_(self.before_proj.weight)
            nn.init.zeros_(self.before_proj.bias)
        self.after_proj = nn.Linear(self.dim, self.dim)
        nn.init.zeros_(self.after_proj.weight)
        nn.init.zeros_(self.after_proj.bias)

    def forward(self, hints, x, **kwargs):
        # behold dbm magic !
        c = hints[0]
        hints[0] = None
        if self.block_id == 0:
            c = self.before_proj(c) + x
        c = super().forward(c, **kwargs)
        c_skip = self.after_proj(c)
        hints[0] = c
        return c_skip

    # def forward(self, c, x, **kwargs):
    #     # behold dbm magic !
    #     if self.block_id == 0:
    #         c = self.before_proj(c) + x
    #         all_c = []
    #     else:
    #         all_c = c
    #         c = all_c.pop(-1)
    #     c = super().forward(c, **kwargs)
    #     c_skip = self.after_proj(c)
    #     all_c += [c_skip, c]
    #     return all_c
    
class Head(nn.Module):

    def __init__(self, dim, out_dim, patch_size, eps=1e-6):
        super().__init__()
        self.dim = dim
        self.out_dim = out_dim
        self.patch_size = patch_size
        self.eps = eps

        # layers
        out_dim = math.prod(patch_size) * out_dim
        self.norm = WanLayerNorm(dim, eps)
        self.head = nn.Linear(dim, out_dim)

        # modulation
        self.modulation = nn.Parameter(torch.randn(1, 2, dim) / dim**0.5)

    def forward(self, x, e):
        r"""
        Args:
            x(Tensor): Shape [B, L1, C]
            e(Tensor): Shape [B, C]
        """
        # assert e.dtype == torch.float32

        e = (self.modulation + e.unsqueeze(1)).chunk(2, dim=1)
        x = self.norm(x).to(torch.bfloat16)
        x *= (1 + e[1])
        x += e[0]
        x = self.head(x)
        return x


class MLPProj(torch.nn.Module):

    def __init__(self, in_dim, out_dim):
        super().__init__()

        self.proj = torch.nn.Sequential(
            torch.nn.LayerNorm(in_dim), torch.nn.Linear(in_dim, in_dim),
            torch.nn.GELU(), torch.nn.Linear(in_dim, out_dim),
            torch.nn.LayerNorm(out_dim))

    def forward(self, image_embeds):
        clip_extra_context_tokens = self.proj(image_embeds)
        return clip_extra_context_tokens


class WanModel(ModelMixin, ConfigMixin):
    r"""
    Wan diffusion backbone supporting both text-to-video and image-to-video.
    """

    ignore_for_config = [
        'patch_size', 'cross_attn_norm', 'qk_norm', 'text_dim', 'window_size'
    ]
    _no_split_modules = ['WanAttentionBlock']

    @register_to_config
    def __init__(self,
                 vace_layers=None,
                 vace_in_dim=None,                 
                 model_type='t2v',
                 patch_size=(1, 2, 2),
                 text_len=512,
                 in_dim=16,
                 dim=2048,
                 ffn_dim=8192,
                 freq_dim=256,
                 text_dim=4096,
                 out_dim=16,
                 num_heads=16,
                 num_layers=32,
                 window_size=(-1, -1),
                 qk_norm=True,
                 cross_attn_norm=True,
                 eps=1e-6,
                 ):
        r"""
        Initialize the diffusion model backbone.

        Args:
            model_type (`str`, *optional*, defaults to 't2v'):
                Model variant - 't2v' (text-to-video) or 'i2v' (image-to-video)
            patch_size (`tuple`, *optional*, defaults to (1, 2, 2)):
                3D patch dimensions for video embedding (t_patch, h_patch, w_patch)
            text_len (`int`, *optional*, defaults to 512):
                Fixed length for text embeddings
            in_dim (`int`, *optional*, defaults to 16):
                Input video channels (C_in)
            dim (`int`, *optional*, defaults to 2048):
                Hidden dimension of the transformer
            ffn_dim (`int`, *optional*, defaults to 8192):
                Intermediate dimension in feed-forward network
            freq_dim (`int`, *optional*, defaults to 256):
                Dimension for sinusoidal time embeddings
            text_dim (`int`, *optional*, defaults to 4096):
                Input dimension for text embeddings
            out_dim (`int`, *optional*, defaults to 16):
                Output video channels (C_out)
            num_heads (`int`, *optional*, defaults to 16):
                Number of attention heads
            num_layers (`int`, *optional*, defaults to 32):
                Number of transformer blocks
            window_size (`tuple`, *optional*, defaults to (-1, -1)):
                Window size for local attention (-1 indicates global attention)
            qk_norm (`bool`, *optional*, defaults to True):
                Enable query/key normalization
            cross_attn_norm (`bool`, *optional*, defaults to False):
                Enable cross-attention normalization
            eps (`float`, *optional*, defaults to 1e-6):
                Epsilon value for normalization layers
        """

        super().__init__()

        assert model_type in ['t2v', 'i2v']
        self.model_type = model_type

        self.patch_size = patch_size
        self.text_len = text_len
        self.in_dim = in_dim
        self.dim = dim
        self.ffn_dim = ffn_dim
        self.freq_dim = freq_dim
        self.text_dim = text_dim
        self.out_dim = out_dim
        self.num_heads = num_heads
        self.num_layers = num_layers
        self.window_size = window_size
        self.qk_norm = qk_norm
        self.cross_attn_norm = cross_attn_norm
        self.eps = eps

        # embeddings
        self.patch_embedding = nn.Conv3d(
            in_dim, dim, kernel_size=patch_size, stride=patch_size)
        self.text_embedding = nn.Sequential(
            nn.Linear(text_dim, dim), nn.GELU(approximate='tanh'),
            nn.Linear(dim, dim))

        self.time_embedding = nn.Sequential(
            nn.Linear(freq_dim, dim), nn.SiLU(), nn.Linear(dim, dim))
        self.time_projection = nn.Sequential(nn.SiLU(), nn.Linear(dim, dim * 6))

        # blocks
        if vace_layers == None:
            cross_attn_type = 't2v_cross_attn' if model_type == 't2v' else 'i2v_cross_attn'
            self.blocks = nn.ModuleList([
                WanAttentionBlock(cross_attn_type, dim, ffn_dim, num_heads,
                                window_size, qk_norm, cross_attn_norm, eps)
                for _ in range(num_layers)
            ])

        # head
        self.head = Head(dim, out_dim, patch_size, eps)

        # buffers (don't use register_buffer otherwise dtype will be changed in to())

        if model_type == 'i2v':
            self.img_emb = MLPProj(1280, dim)

        # initialize weights
        self.init_weights()

        if vace_layers != None:            
            self.vace_layers = [i for i in range(0, self.num_layers, 2)] if vace_layers is None else vace_layers
            self.vace_in_dim = self.in_dim if vace_in_dim is None else vace_in_dim

            assert 0 in self.vace_layers
            self.vace_layers_mapping = {i: n for n, i in enumerate(self.vace_layers)}

            # blocks
            self.blocks = nn.ModuleList([
                WanAttentionBlock('t2v_cross_attn', self.dim, self.ffn_dim, self.num_heads, self.window_size, self.qk_norm,
                                    self.cross_attn_norm, self.eps,
                                    block_id=self.vace_layers_mapping[i] if i in self.vace_layers else None)
                for i in range(self.num_layers)
            ])

            # vace blocks
            self.vace_blocks = nn.ModuleList([
                VaceWanAttentionBlock('t2v_cross_attn', self.dim, self.ffn_dim, self.num_heads, self.window_size, self.qk_norm,
                                        self.cross_attn_norm, self.eps, block_id=i)
                for i in self.vace_layers
            ])

            # vace patch embeddings
            self.vace_patch_embedding = nn.Conv3d(
                self.vace_in_dim, self.dim, kernel_size=self.patch_size, stride=self.patch_size
            )


    def compute_teacache_threshold(self, start_step, timesteps = None, speed_factor =0):
        rescale_func = np.poly1d(self.coefficients)         
        e_list = []
        for t in timesteps:
            t = torch.stack([t])
            e_list.append(self.time_embedding( sinusoidal_embedding_1d(self.freq_dim, t)))
	
        best_threshold = 0.01
        best_diff = 1000
        best_signed_diff = 1000
        target_nb_steps= int(len(timesteps) / speed_factor)
        threshold = 0.01
        while threshold <= 0.6:
            accumulated_rel_l1_distance =0
            nb_steps = 0
            diff = 1000
            for i, t in enumerate(timesteps):
                skip = False
                if not (i<=start_step or i== len(timesteps)):
                    accumulated_rel_l1_distance += rescale_func(((e_list[i]-previous_modulated_input).abs().mean() / previous_modulated_input.abs().mean()).cpu().item())
        #   self.accumulated_rel_l1_distance_even += rescale_func(((e_list[i]-self.previous_e0_even).abs().mean() / self.previous_e0_even.abs().mean()).cpu().item())

                    if accumulated_rel_l1_distance < threshold:
                        skip = True
                    else:
                        accumulated_rel_l1_distance = 0
                previous_modulated_input = e_list[i]
                if not skip:
                    nb_steps += 1
                    signed_diff = target_nb_steps - nb_steps               
                    diff = abs(signed_diff)  
            if diff < best_diff:
                best_threshold = threshold
                best_diff = diff
                best_signed_diff = signed_diff
            elif diff > best_diff:
                break
            threshold += 0.01
        self.rel_l1_thresh = best_threshold
        print(f"Tea Cache, best threshold found:{best_threshold:0.2f} with gain x{len(timesteps)/(target_nb_steps - best_signed_diff):0.2f} for a target of x{speed_factor}")
        return best_threshold



    # def forward_vace(
    #     self,
    #     x,
    #     vace_context,
    #     seq_len,
    #     context,
    #     e,
    #     kwargs
    # ):
    #     # embeddings
    #     c = [self.vace_patch_embedding(u.unsqueeze(0)) for u in vace_context]
    #     c = [u.flatten(2).transpose(1, 2) for u in c]
    #     if (len(c) == 1 and seq_len == c[0].size(1)):
    #         c = c[0]
    #     else:
    #         c = torch.cat([
    #             torch.cat([u, u.new_zeros(1, seq_len - u.size(1), u.size(2))],
    #                     dim=1) for u in c
    #         ])

    #     # arguments
    #     new_kwargs = dict(x=x)
    #     new_kwargs.update(kwargs)

    #     for block in self.vace_blocks:
    #         c = block(c, context= context, e= e, **new_kwargs)
    #     hints = c[:-1]

    #     return hints
    
    def forward(
        self,
        x,
        t,
        context,
        seq_len,
        vace_context = None,
        vace_context_scale=1.0,        
        clip_fea=None,
        y=None,
        freqs = None,
        pipeline = None,
        current_step = 0,
        context2 = None,
        is_uncond=False,
        max_steps = 0, 
        slg_layers=None,
        callback = None,
    ):
        r"""
        Forward pass through the diffusion model

        Args:
            x (List[Tensor]):
                List of input video tensors, each with shape [C_in, F, H, W]
            t (Tensor):
                Diffusion timesteps tensor of shape [B]
            context (List[Tensor]):
                List of text embeddings each with shape [L, C]
            seq_len (`int`):
                Maximum sequence length for positional encoding
            clip_fea (Tensor, *optional*):
                CLIP image features for image-to-video mode
            y (List[Tensor], *optional*):
                Conditional video inputs for image-to-video mode, same shape as x

        Returns:
            List[Tensor]:
                List of denoised video tensors with original input shapes [C_out, F, H / 8, W / 8]
        """
        if self.model_type == 'i2v':
            assert clip_fea is not None and y is not None
        # params
        device = self.patch_embedding.weight.device
        if torch.is_tensor(freqs) and freqs.device != device:
            freqs = freqs.to(device)

        if y is not None:
            x = [torch.cat([u, v], dim=0) for u, v in zip(x, y)]

        # embeddings
        x = [self.patch_embedding(u.unsqueeze(0)) for u in x]
        # grid_sizes = torch.stack(
        #     [torch.tensor(u.shape[2:], dtype=torch.long) for u in x])

        grid_sizes = [ list(u.shape[2:]) for u in x]
        embed_sizes = grid_sizes[0]

        offload.shared_state["embed_sizes"] = embed_sizes 
        offload.shared_state["step_no"] = current_step 
        offload.shared_state["max_steps"] = max_steps


        x = [u.flatten(2).transpose(1, 2) for u in x]
        seq_lens = torch.tensor([u.size(1) for u in x], dtype=torch.long)
        assert seq_lens.max() <= seq_len
        if len(x)==1 and seq_len == x[0].size(1):
            x = x[0]
        else:
            x = torch.cat([
                torch.cat([u, u.new_zeros(1, seq_len - u.size(1), u.size(2))],
                        dim=1) for u in x
            ])

        # time embeddings
        e = self.time_embedding(
            sinusoidal_embedding_1d(self.freq_dim, t))
        e0 = self.time_projection(e).unflatten(1, (6, self.dim)).to(torch.bfloat16)

        # context
        context_lens = None
        context = self.text_embedding(
            torch.stack([
                torch.cat(
                    [u, u.new_zeros(self.text_len - u.size(0), u.size(1))])
                for u in context
            ]))
        if context2!=None:
            context2 = self.text_embedding(
                torch.stack([
                    torch.cat(
                        [u, u.new_zeros(self.text_len - u.size(0), u.size(1))])
                    for u in context2
                ]))

        if clip_fea is not None:
            context_clip = self.img_emb(clip_fea)  # bs x 257 x dim
            context = torch.concat([context_clip, context], dim=1)
            if context2 != None:
                context2 = torch.concat([context_clip, context2], dim=1)
        
        joint_pass = context2 != None
        if joint_pass:
            x_list = [x, x.clone()]
            context_list = [context, context2]
            is_uncond = False
        else:
            x_list = [x]
            context_list = [context]
        del x

            # arguments

        kwargs = dict(
            seq_lens=seq_lens,
            grid_sizes=grid_sizes,
            freqs=freqs,
            context_lens=context_lens,
            )

        if vace_context == None:
            hints_list = [None ] *len(x_list)
        else:
            # embeddings
            c = [self.vace_patch_embedding(u.unsqueeze(0)) for u in vace_context]
            c = [u.flatten(2).transpose(1, 2) for u in c]
            if (len(c) == 1 and seq_len == c[0].size(1)):
                c = c[0]
            else:
                c = torch.cat([
                    torch.cat([u, u.new_zeros(1, seq_len - u.size(1), u.size(2))],
                            dim=1) for u in c
                ])
 
            kwargs['context_scale'] = vace_context_scale
            hints_list = [ [c] for _ in range(len(x_list)) ] 
            del c

        should_calc = True
        if self.enable_teacache: 
            if is_uncond:
                should_calc = self.should_calc
            else:
                if current_step <= self.teacache_start_step or current_step == self.num_steps-1:
                    should_calc = True
                    self.accumulated_rel_l1_distance = 0
                else:
                    rescale_func = np.poly1d(self.coefficients)
                    self.accumulated_rel_l1_distance += rescale_func(((e-self.previous_modulated_input).abs().mean() / self.previous_modulated_input.abs().mean()).cpu().item())
                    if self.accumulated_rel_l1_distance < self.rel_l1_thresh:
                        should_calc = False
                        self.teacache_skipped_steps += 1
                        # print(f"Teacache Skipped Step:{self.teacache_skipped_steps}/{current_step}" )
                    else:
                        should_calc = True
                        self.accumulated_rel_l1_distance = 0
                self.previous_modulated_input = e 
                self.should_calc = should_calc                        

        if not should_calc:
            for i, x in enumerate(x_list):
                x += self.previous_residual_uncond if i==1 or is_uncond else self.previous_residual_cond                              
        else:
            if self.enable_teacache:
                if joint_pass or is_uncond:
                    self.previous_residual_uncond = None
                if joint_pass or not is_uncond:
                    self.previous_residual_cond = None
                ori_hidden_states = x_list[0].clone()
            
            for block_idx, block in enumerate(self.blocks):
                offload.shared_state["layer"] = block_idx
                if callback != None:
                    callback(-1, False, True)
                if pipeline._interrupt:
                    if joint_pass:
                        return None, None
                    else:
                        return [None]

                if slg_layers is not None and block_idx in slg_layers:
                    if is_uncond and not joint_pass:
                        continue
                    x_list[0] = block(x_list[0], context = context_list[0], e= e0, **kwargs)

                else:
                    for i, (x, context, hints) in enumerate(zip(x_list, context_list, hints_list)):
                        x_list[i] = block(x, context = context, hints= hints, e= e0, **kwargs)
                        del x
                    del context, hints

            if self.enable_teacache:
                if joint_pass:
                    self.previous_residual_cond = torch.sub(x_list[0], ori_hidden_states)
                    self.previous_residual_uncond = ori_hidden_states
                    torch.sub(x_list[1], ori_hidden_states, out=self.previous_residual_uncond)
                else:
                    residual = ori_hidden_states # just to have a readable code
                    torch.sub(x_list[0], ori_hidden_states, out=residual)
                    if i==1 or is_uncond:
                        self.previous_residual_uncond = residual
                    else:
                        self.previous_residual_cond = residual
                residual, ori_hidden_states = None, None

        for i, x in enumerate(x_list):
            # head
            x = self.head(x, e)

            # unpatchify
            x_list[i] = self.unpatchify(x, grid_sizes)
            del x

        if joint_pass:
            return x_list[0][0], x_list[1][0]
        else:
            return [u.float() for u in x_list[0]]

    def unpatchify(self, x, grid_sizes):
        r"""
        Reconstruct video tensors from patch embeddings.

        Args:
            x (List[Tensor]):
                List of patchified features, each with shape [L, C_out * prod(patch_size)]
            grid_sizes (Tensor):
                Original spatial-temporal grid dimensions before patching,
                    shape [B, 3] (3 dimensions correspond to F_patches, H_patches, W_patches)

        Returns:
            List[Tensor]:
                Reconstructed video tensors with shape [C_out, F, H / 8, W / 8]
        """

        c = self.out_dim
        out = []
        for u, v in zip(x, grid_sizes):
            u = u[:math.prod(v)].view(*v, *self.patch_size, c)
            u = torch.einsum('fhwpqrc->cfphqwr', u)
            u = u.reshape(c, *[i * j for i, j in zip(v, self.patch_size)])
            out.append(u)
        return out

    def init_weights(self):
        r"""
        Initialize model parameters using Xavier initialization.
        """

        # basic init
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

        # init embeddings
        nn.init.xavier_uniform_(self.patch_embedding.weight.flatten(1))
        for m in self.text_embedding.modules():
            if isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, std=.02)
        for m in self.time_embedding.modules():
            if isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, std=.02)

        # init output layer
        nn.init.zeros_(self.head.head.weight)
