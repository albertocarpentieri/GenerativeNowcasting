"""
From https://github.com/MeteoSwiss/ldcast/blob/master/ldcast/models/blocks/afno.py
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


class AFNO3D(nn.Module):
    def __init__(
            self, hidden_size, num_blocks=8, sparsity_threshold=0.01,
            hard_thresholding_fraction=1, hidden_size_factor=1, res_mult=1
    ):
        super().__init__()
        assert hidden_size % num_blocks == 0, f"hidden_size {hidden_size} should be divisble by num_blocks {num_blocks}"

        self.hidden_size = hidden_size
        self.sparsity_threshold = sparsity_threshold
        self.num_blocks = num_blocks
        self.block_size = self.hidden_size // self.num_blocks
        self.hard_thresholding_fraction = hard_thresholding_fraction
        self.hidden_size_factor = hidden_size_factor
        self.scale = 0.02
        self.res_mult = res_mult

        self.w1 = nn.Parameter(
            self.scale * torch.randn(2, self.num_blocks, self.block_size, self.block_size * self.hidden_size_factor))
        self.b1 = nn.Parameter(self.scale * torch.randn(2, self.num_blocks, self.block_size * self.hidden_size_factor))
        self.w2 = nn.Parameter(
            self.scale * torch.randn(2, self.num_blocks, self.block_size * self.hidden_size_factor, self.block_size))
        self.b2 = nn.Parameter(self.scale * torch.randn(2, self.num_blocks, self.block_size))

    def forward(self, x):
        bias = x

        dtype = x.dtype
        x = x.float()
        B, D, H, W, C = x.shape

        x = torch.fft.rfftn(x, dim=(1, 2, 3), norm="ortho")
        x = x.reshape(B, D, H, W // 2 + 1, self.num_blocks, self.block_size)

        o1_real = torch.zeros([B, D, H, W // 2 + 1, self.num_blocks, self.block_size * self.hidden_size_factor],
                              device=x.device)
        o1_imag = torch.zeros([B, D, H, W // 2 + 1, self.num_blocks, self.block_size * self.hidden_size_factor],
                              device=x.device)
        o2_real = torch.zeros(x.shape, device=x.device)
        o2_imag = torch.zeros(x.shape, device=x.device)

        total_modes = H // 2 + 1
        kept_modes = int(total_modes * self.hard_thresholding_fraction)

        o1_real[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes] = F.relu(
            torch.einsum('...bi,bio->...bo',
                         x[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes].real, self.w1[0]) -
            torch.einsum('...bi,bio->...bo',
                         x[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes].imag, self.w1[1]) +
            self.b1[0]
        )

        o1_imag[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes] = F.relu(
            torch.einsum('...bi,bio->...bo',
                         x[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes].imag, self.w1[0]) +
            torch.einsum('...bi,bio->...bo',
                         x[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes].real, self.w1[1]) +
            self.b1[1]
        )

        o2_real[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes] = (
                torch.einsum('...bi,bio->...bo',
                             o1_real[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes],
                             self.w2[0]) -
                torch.einsum('...bi,bio->...bo',
                             o1_imag[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes],
                             self.w2[1]) +
                self.b2[0]
        )

        o2_imag[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes] = (
                torch.einsum('...bi,bio->...bo',
                             o1_imag[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes],
                             self.w2[0]) +
                torch.einsum('...bi,bio->...bo',
                             o1_real[:, :, total_modes - kept_modes:total_modes + kept_modes, :kept_modes],
                             self.w2[1]) +
                self.b2[1]
        )

        x = torch.stack([o2_real, o2_imag], dim=-1)
        x = F.softshrink(x, lambd=self.sparsity_threshold)
        x = torch.view_as_complex(x)
        x = x.reshape(B, D, H, W // 2 + 1, C)
        x = torch.fft.irfftn(x, s=(D, H*self.res_mult, W*self.res_mult), dim=(1, 2, 3), norm="ortho")
        x = x.type(dtype)
        if self.res_mult>1:
            return x
        else:
            return x + bias


class Mlp(nn.Module):
    def __init__(
            self,
            in_features, hidden_features=None, out_features=None,
            act_layer=nn.GELU, drop=0.0
    ):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop) if drop > 0 else nn.Identity()

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class AFNOBlock3d(nn.Module):
    def __init__(
            self,
            dim,
            mlp_ratio=4.,
            drop=0.,
            act_layer=nn.GELU,
            norm_layer=nn.LayerNorm,
            double_skip=True,
            num_blocks=8,
            sparsity_threshold=0.01,
            hard_thresholding_fraction=1.0,
            data_format="channels_last",
            mlp_out_features=None,
            afno_res_mult=1,

    ):
        super().__init__()
        self.norm_layer = norm_layer
        self.afno_res_mult = afno_res_mult
        self.norm1 = norm_layer(dim)
        self.filter = AFNO3D(dim, num_blocks, sparsity_threshold,
                             hard_thresholding_fraction, res_mult=afno_res_mult)
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(
            in_features=dim, out_features=mlp_out_features,
            hidden_features=mlp_hidden_dim,
            act_layer=act_layer, drop=drop
        )
        self.double_skip = double_skip
        self.channels_first = (data_format == "channels_first")

    def forward(self, x):
        if self.channels_first:
            # AFNO natively uses a channels-last data format
            x = x.permute(0, 2, 3, 4, 1)

        residual = x
        x = self.norm1(x)
        x = self.filter(x)
        if self.afno_res_mult > 1:
            residual = F.interpolate(residual, x.shape[2:])
        if self.double_skip:
            x = x + residual
            residual = x

        x = self.norm2(x)
        x = self.mlp(x)
        x = x + residual

        if self.channels_first:
            x = x.permute(0, 4, 1, 2, 3)

        return x


class AFNOCrossAttentionBlock3d(nn.Module):
    """ AFNO 3D Block with channel mixing from two sources.
    """

    def __init__(
            self,
            dim,
            context_dim,
            mlp_ratio=2.,
            drop=0.,
            act_layer=nn.GELU,
            norm_layer=nn.Identity,
            double_skip=True,
            num_blocks=8,
            sparsity_threshold=0.01,
            hard_thresholding_fraction=1.0,
            data_format="channels_last",
            timesteps=None
    ):
        super().__init__()

        self.norm1 = norm_layer(dim)
        self.norm2 = norm_layer(dim + context_dim)
        mlp_hidden_dim = int((dim + context_dim) * mlp_ratio)
        self.pre_proj = nn.Linear(dim + context_dim, dim + context_dim)
        self.filter = AFNO3D(dim + context_dim, num_blocks, sparsity_threshold,
                             hard_thresholding_fraction)
        self.mlp = Mlp(
            in_features=dim + context_dim,
            out_features=dim,
            hidden_features=mlp_hidden_dim,
            act_layer=act_layer, drop=drop
        )
        self.channels_first = (data_format == "channels_first")

    def forward(self, x, y):
        if self.channels_first:
            # AFNO natively uses a channels-last order
            x = x.permute(0, 2, 3, 4, 1)
            y = y.permute(0, 2, 3, 4, 1)

        xy = torch.concat((self.norm1(x), y), axis=-1)
        xy = self.pre_proj(xy) + xy
        xy = self.filter(self.norm2(xy)) + xy  # AFNO filter
        x = self.mlp(xy) + x  # feed-forward

        if self.channels_first:
            x = x.permute(0, 4, 1, 2, 3)

        return x