import torch
import torch.nn.functional as F
from torch import nn, einsum

from einops.layers.torch import Rearrange
from einops import rearrange, repeat

# helper functions

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

def divisible_by(numer, denom):
    return (numer % denom) == 0

# tensor helpers

def grid_sample_1d(feats, grid, *args, **kwargs):
    # does 1d grid sample by reshaping it to 2d

    grid = rearrange(grid, '... -> ... 1 1')
    grid = F.pad(grid, (0, 1), value = 0.)
    feats = rearrange(feats, '... -> ... 1')
    out = F.grid_sample(feats, grid, **kwargs)
    return rearrange(out, '... 1 -> ...')

def normalize_grid(arange, dim = 1, out_dim = -1):
    # normalizes 1d sequence to range of -1 to 1
    n = arange.shape[-1]
    return 2.0 * arange / max(n - 1, 1) - 1.0

class Scale(nn.Module):
    def __init__(self, scale):
        super().__init__()
        self.scale = scale

    def forward(self, x):
        return x * self.scale

# continuous positional bias from SwinV2

class CPB(nn.Module):
    """ https://arxiv.org/abs/2111.09883v1 """

    def __init__(self, dim, *, heads, offset_groups, depth):
        super().__init__()
        self.heads = heads
        self.offset_groups = offset_groups

        self.mlp = nn.ModuleList([])

        self.mlp.append(nn.Sequential(
            nn.Linear(1, dim),
            nn.ReLU()
        ))

        for _ in range(depth - 1):
            self.mlp.append(nn.Sequential(
                nn.Linear(dim, dim),
                nn.ReLU()
            ))

        self.mlp.append(nn.Linear(dim, heads // offset_groups))

    def forward(self, grid_q, grid_kv):
        device, dtype = grid_q.device, grid_kv.dtype

        grid_q = rearrange(grid_q, 'n -> 1 n')
        grid_kv = rearrange(grid_kv, 'b n -> b n')

        pos = rearrange(grid_q, 'b i -> b i 1 1') - rearrange(grid_kv, 'b j -> b 1 j 1')
        bias = torch.sign(pos) * torch.log(pos.abs() + 1)  # log of distance is sign(rel_pos) * log(abs(rel_pos) + 1)

        for layer in self.mlp:
            bias = layer(bias)

        bias = rearrange(bias, '(b g) i j o -> b (g o) i j', g = self.offset_groups)

        return bias

# main class

class DeformableAttention1D(nn.Module):
    def __init__(
        self,
        *,
        dim,
        dim_head = 64,
        heads = 8,
        dropout = 0.,
        downsample_factor = 4,
        offset_scale = 4,
        offset_groups = None,
        offset_kernel_size = 6
    ):
        super().__init__()
        assert divisible_by(offset_kernel_size - downsample_factor, 2)

        offset_groups = default(offset_groups, heads)
        assert divisible_by(heads, offset_groups)

        inner_dim = dim_head * heads
        self.scale = dim_head ** -0.5
        self.heads = heads
        self.offset_groups = offset_groups

        offset_dims = inner_dim // offset_groups

        self.downsample_factor = downsample_factor

        self.to_offsets = nn.Sequential(
            Rearrange('b n d -> b d n'),
            nn.Conv1d(offset_dims, offset_dims, offset_kernel_size, groups = offset_dims, stride = downsample_factor, padding = (offset_kernel_size - downsample_factor) // 2),
            nn.GELU(),
            nn.Conv1d(offset_dims, 1, 1, bias = False),
            Rearrange('b 1 n -> b n'),
            nn.Tanh(),
            Scale(offset_scale)
        )

        self.rel_pos_bias = CPB(dim // 4, offset_groups = offset_groups, heads = heads, depth = 2)

        self.dropout = nn.Dropout(dropout)
        self.to_q = nn.Linear(dim, inner_dim, bias = False)
        self.to_kv = nn.Linear(dim, inner_dim * 2, bias = False)
        self.to_out = nn.Linear(inner_dim, dim)

    def forward(self, x, return_vgrid = False):
        """
        b - batch
        h - heads
        x - height
        y - width
        d - dimension
        g - offset groups
        """

        heads, b, n, downsample_factor, device = self.heads, x.shape[0], x.shape[-2], self.downsample_factor, x.device

        # queries

        q = self.to_q(x)

        # calculate offsets - offset MLP shared across all groups

        grouped_feats = rearrange(q, 'b n (g d) -> (b g) d n', g = self.offset_groups)
        offsets = self.to_offsets(grouped_feats)

        # calculate grid + offsets

        grid = torch.arange(offsets.shape[-1], device = device)
        vgrid = grid + offsets
        vgrid_scaled = normalize_grid(vgrid)

        kv_feats = grid_sample_1d(
            grouped_feats,
            vgrid_scaled,
        mode = 'bilinear', padding_mode = 'zeros', align_corners = False)

        kv_feats = rearrange(kv_feats, '(b g) d n -> b n (g d)', b = b)

        # derive key / values

        k, v = self.to_kv(kv_feats).chunk(2, dim = -1)

        # scale queries

        q = q * self.scale

        # split out heads

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = heads), (q, k, v))

        # query / key similarity

        sim = einsum('b h i d, b h j d -> b h i j', q, k)

        # relative positional bias

        seq_range = torch.arange(n, device = device)
        seq_scaled = normalize_grid(seq_range, dim = 0)
        rel_pos_bias = self.rel_pos_bias(seq_scaled, vgrid_scaled)
        sim = sim + rel_pos_bias

        # numerical stability

        sim = sim - sim.amax(dim = -1, keepdim = True).detach()

        # attention

        attn = sim.softmax(dim = -1)
        attn = self.dropout(attn)

        # aggregate and combine heads

        out = einsum('b h i j, b h j d -> b h i d', attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        out = self.to_out(out)

        if return_vgrid:
            return out, vgrid

        return out