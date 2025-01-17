import os.path

import torch
import torch.nn as nn
import torch.nn.functional as F

import struct

from encoding import get_encoder
from activation import trunc_exp
from .renderer import NeRFRenderer

import raymarching


class NeRFNetwork(NeRFRenderer):
    def __init__(self,
                 encoding="hashgrid",
                 encoding_dir="sphere_harmonics",
                 encoding_bg="hashgrid",
                 num_layers=2,
                 hidden_dim=64,
                 geo_feat_dim=15,
                 num_layers_color=3,
                 hidden_dim_color=64,
                 num_layers_bg=2,
                 hidden_dim_bg=64,
                 bound=1,
                 **kwargs,
                 ):
        super().__init__(bound, **kwargs)

        # sigma network
        self.num_layers = num_layers
        self.hidden_dim = hidden_dim
        self.geo_feat_dim = geo_feat_dim
        self.encoder, self.in_dim = get_encoder(encoding, desired_resolution=1024)

        sigma_net = []
        for l in range(num_layers):
            if l == 0:
                in_dim = self.in_dim
            else:
                in_dim = hidden_dim

            if l == num_layers - 1:
                out_dim = 1 + self.geo_feat_dim # 1 sigma + 15 SH features for color
            else:
                out_dim = hidden_dim

            sigma_net.append(nn.Linear(in_dim, out_dim, bias=False))

        self.sigma_net = nn.ModuleList(sigma_net)

        # color network
        self.num_layers_color = num_layers_color
        self.hidden_dim_color = hidden_dim_color
        self.encoder_dir, self.in_dim_dir = get_encoder(encoding_dir)

        color_net =  []
        for l in range(num_layers_color):
            if l == 0:
                in_dim = self.in_dim_dir + self.geo_feat_dim
            else:
                in_dim = hidden_dim_color

            if l == num_layers_color - 1:
                out_dim = 3 # 3 rgb
            else:
                out_dim = hidden_dim_color

            color_net.append(nn.Linear(in_dim, out_dim, bias=False))

        self.color_net = nn.ModuleList(color_net)

        # background network
        if self.bg_radius > 0:
            self.num_layers_bg = num_layers_bg
            self.hidden_dim_bg = hidden_dim_bg
            self.encoder_bg, self.in_dim_bg = get_encoder(encoding_bg, input_dim=2, num_levels=4, log2_hashmap_size=19, desired_resolution=2048) # much smaller hashgrid

            bg_net = []
            for l in range(num_layers_bg):
                if l == 0:
                    in_dim = self.in_dim_bg + self.in_dim_dir
                else:
                    in_dim = hidden_dim_bg

                if l == num_layers_bg - 1:
                    out_dim = 3 # 3 rgb
                else:
                    out_dim = hidden_dim_bg

                bg_net.append(nn.Linear(in_dim, out_dim, bias=False))

            self.bg_net = nn.ModuleList(bg_net)
        else:
            self.bg_net = None


    def forward(self, x, d):
        # x: [N, 3], in [-bound, bound]
        # d: [N, 3], nomalized in [-1, 1]

        # sigma
        x = self.encoder(x, bound=self.bound)

        h = x
        for l in range(self.num_layers):
            h = self.sigma_net[l](h)
            h = F.relu(h, inplace=True)

        #sigma = F.relu(h[..., 0])
        sigma = h[..., 0]
        geo_feat = h[..., 1:]

        # color

        d = self.encoder_dir(d)
        h = torch.cat([geo_feat, d], dim=-1)
        for l in range(self.num_layers_color):
            h = self.color_net[l](h)
            if l != self.num_layers_color - 1:
                h = F.relu(h, inplace=True)

        # sigmoid activation for rgb
        color = torch.clip(h, -8, 8) * (1 / 16) + 0.5

        return sigma, color

    def density(self, x):
        # x: [N, 3], in [-bound, bound]

        x = self.encoder(x, bound=self.bound)
        h = x
        for l in range(self.num_layers):
            h = self.sigma_net[l](h)
            h = F.relu(h, inplace=True)

        #sigma = F.relu(h[..., 0])
        sigma = h[..., 0]
        geo_feat = h[..., 1:]

        return {
            'sigma': sigma,
            'geo_feat': geo_feat,
        }

    def background(self, x, d):
        # x: [N, 2], in [-1, 1]

        h = self.encoder_bg(x) # [N, C]
        d = self.encoder_dir(d)

        h = torch.cat([d, h], dim=-1)
        for l in range(self.num_layers_bg):
            h = self.bg_net[l](h)
            if l != self.num_layers_bg - 1:
                h = F.relu(h, inplace=True)

        # sigmoid activation for rgb
        rgbs = torch.sigmoid(h)

        return rgbs

    # allow masked inference
    def color(self, x, d, mask=None, geo_feat=None, **kwargs):
        # x: [N, 3] in [-bound, bound]
        # mask: [N,], bool, indicates where we actually needs to compute rgb.

        if mask is not None:
            rgbs = torch.zeros(mask.shape[0], 3, dtype=x.dtype, device=x.device) # [N, 3]
            # in case of empty mask
            if not mask.any():
                return rgbs
            x = x[mask]
            d = d[mask]
            geo_feat = geo_feat[mask]

        d = self.encoder_dir(d)
        h = torch.cat([geo_feat, d], dim=-1)
        for l in range(self.num_layers_color):
            h = self.color_net[l](h)
            if l != self.num_layers_color - 1:
                h = F.relu(h, inplace=True)

        # sigmoid activation for rgb
        h = torch.clip(h, -8, 8) * (1 / 16) + 0.5

        if mask is not None:
            rgbs[mask] = h.to(rgbs.dtype) # fp16 --> fp32
        else:
            rgbs = h

        return rgbs

    # optimizer utils
    def get_params(self, lr):

        params = [
            {'params': self.encoder.parameters(), 'lr': lr},
            {'params': self.sigma_net.parameters(), 'lr': lr},
            {'params': self.encoder_dir.parameters(), 'lr': lr},
            {'params': self.color_net.parameters(), 'lr': lr},
        ]
        if self.bg_radius > 0:
            params.append({'params': self.encoder_bg.parameters(), 'lr': lr})
            params.append({'params': self.bg_net.parameters(), 'lr': lr})

        return params


    def save_hardware_parameters(self, path):
        if(os.path.exists(path)):
            os.remove(path)

        fid = open(path, 'wb')

        with torch.no_grad():

            grids = torch.clip(torch.round(self.encoder.embeddings * 1024).to(dtype = torch.int), -32768, 32767).view(-1).tolist()
            offsets = self.encoder.offsets

            for i in range(len(offsets) - 1):
                num = struct.pack('i', (offsets[i + 1] - offsets[i]) * 4 * 2)
                fid.write(num)
                num = struct.pack(f'{(offsets[i + 1] - offsets[i]) * 4}h', *grids[4 * offsets[i]:4 * offsets[i + 1]])
                fid.write(num)

            weight_now = torch.clip(torch.round(self.sigma_net[0].weight * 1024).to(dtype=torch.int), -32768, 32767).view(-1).tolist()
            num = struct.pack('i', len(weight_now) * 2)
            fid.write(num)
            num = struct.pack('2048h', *weight_now)
            fid.write(num)

            weight_now = torch.clip(torch.round(self.sigma_net[1].weight * 1024).to(dtype=torch.int), -32768, 32767)
            weight_now = torch.concat([weight_now[:, 0:16], weight_now[:, 16:32], weight_now[:, 32:48], weight_now[:, 48:64]], 0).view(-1).tolist()
            num = struct.pack('i', len(weight_now) * 2)
            fid.write(num)
            num = struct.pack('1024h', *weight_now)
            fid.write(num)

            weight_now = torch.clip(torch.round(self.color_net[0].weight * 1024).to(dtype=torch.int), -32768, 32767)
            weight_now = torch.concat([torch.zeros([64, 1], dtype=torch.int, device=weight_now.device), weight_now], 1).view(-1).tolist()
            num = struct.pack('i', len(weight_now) * 2)
            fid.write(num)
            num = struct.pack('2048h', *weight_now)
            fid.write(num)

            weight_now = torch.clip(torch.round(self.color_net[1].weight * 1024).to(dtype=torch.int), -32768, 32767).view(-1).tolist()
            num = struct.pack('i', len(weight_now) * 2)
            fid.write(num)
            num = struct.pack('4096h', *weight_now)
            fid.write(num)

            weight_now = torch.transpose(torch.clip(torch.round(self.color_net[2].weight * 1024), -32768, 32767).to(dtype=torch.int), 1, 0)
            weight_now = torch.concat([weight_now, torch.zeros([64, 1], dtype=torch.int, device=weight_now.device)], 1).view(-1).tolist()
            num = struct.pack('i', len(weight_now) * 2)
            fid.write(num)
            num = struct.pack('256h', *weight_now)
            fid.write(num)

            bitfield_compressed = raymarching.compress_bitfiled(self.density_bitfield, 32).tolist()
            num = struct.pack('i', len(bitfield_compressed))
            fid.write(num)
            num = struct.pack(f'{len(bitfield_compressed)}B', *bitfield_compressed)
            fid.write(num)

        fid.close()