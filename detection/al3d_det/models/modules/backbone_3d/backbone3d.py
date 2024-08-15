from functools import partial

import spconv.pytorch as spconv
import torch.nn as nn

norm_cfg = {
    # format: layer_type: (abbreviation, module)
    "BN": ("bn", nn.BatchNorm2d),
    "BN1d": ("bn1d", nn.BatchNorm1d),
    "GN": ("gn", nn.GroupNorm),
}

def build_norm_layer(cfg, num_features, postfix=""):
    """ Build normalization layer
    Args:
        cfg (dict): cfg should contain:
            type (str): identify norm layer type.
            layer args: args needed to instantiate a norm layer.
            requires_grad (bool): [optional] whether stop gradient updates
        num_features (int): number of channels from input.
        postfix (int, str): appended into norm abbreviation to
            create named layer.
    Returns:
        name (str): abbreviation + postfix
        layer (nn.Module): created norm layer
    """
    assert isinstance(cfg, dict) and "type" in cfg
    cfg_ = cfg.copy()

    layer_type = cfg_.pop("type")
    if layer_type not in norm_cfg:
        raise KeyError("Unrecognized norm type {}".format(layer_type))
    else:
        abbr, norm_layer = norm_cfg[layer_type]
        if norm_layer is None:
            raise NotImplementedError

    assert isinstance(postfix, (int, str))
    name = abbr + str(postfix)

    requires_grad = cfg_.pop("requires_grad", True)
    cfg_.setdefault("eps", 1e-5)
    if layer_type != "GN":
        layer = norm_layer(num_features, **cfg_)
        # if layer_type == 'SyncBN':
        #     layer._specify_ddp_gpu_num(1)
    else:
        assert "num_groups" in cfg_
        layer = norm_layer(num_channels=num_features, **cfg_)

    for param in layer.parameters():
        param.requires_grad = requires_grad

    return name, layer

def post_act_block(in_channels, out_channels, kernel_size, indice_key=None, stride=1, padding=0,
                   conv_type='subm', norm_fn=None):

    if conv_type == 'subm':
        conv = spconv.SubMConv3d(in_channels, out_channels, kernel_size, bias=False, indice_key=indice_key)
    elif conv_type == 'spconv':
        conv = spconv.SparseConv3d(in_channels, out_channels, kernel_size, stride=stride, padding=padding,
                                   bias=False, indice_key=indice_key)
    elif conv_type == 'inverseconv':
        conv = spconv.SparseInverseConv3d(in_channels, out_channels, kernel_size, indice_key=indice_key, bias=False)
    else:
        raise NotImplementedError

    m = spconv.SparseSequential(
        conv,
        build_norm_layer(norm_fn, out_channels)[1],
        nn.ReLU(),
    )

    return m


class Backbone3D(nn.Module):
    def __init__(self, model_cfg, input_channels, grid_size, **kwargs):
        super().__init__()
        self.model_cfg = model_cfg
        channels = getattr(model_cfg, "CHANNELS", [16, 32, 64, 128])
        # norm_fn = partial(nn.BatchNorm1d, eps=1e-3, momentum=0.01)
        norm_fn = dict(type="BN1d", eps=1e-3, momentum=0.01)
        self.sparse_shape = grid_size[::-1] + [1, 0, 0]

        self.conv_input = spconv.SparseSequential(
            spconv.SubMConv3d(input_channels, channels[0], 3, padding=1, bias=False, indice_key='subm1'),
            build_norm_layer(norm_fn, channels[0])[1],
            nn.ReLU(),
        )
        block = post_act_block

        self.conv1 = spconv.SparseSequential(
            block(channels[0], channels[0], 3, norm_fn=norm_fn, padding=1, indice_key='subm1'),
        )

        self.conv2 = spconv.SparseSequential(
            # [1600, 1408, 41] <- [800, 704, 21]
            block(channels[0], channels[1], 3, norm_fn=norm_fn, stride=2, padding=1, indice_key='spconv2',
                  conv_type='spconv'),
            block(channels[1], channels[1], 3, norm_fn=norm_fn, padding=1, indice_key='subm2'),
            block(channels[1], channels[1], 3, norm_fn=norm_fn, padding=1, indice_key='subm2'),
        )

        self.conv3 = spconv.SparseSequential(
            # [800, 704, 21] <- [400, 352, 11]
            block(channels[1], channels[2], 3, norm_fn=norm_fn, stride=2, padding=1, indice_key='spconv3',
                  conv_type='spconv'),
            block(channels[2], channels[2], 3, norm_fn=norm_fn, padding=1, indice_key='subm3'),
            block(channels[2], channels[2], 3, norm_fn=norm_fn, padding=1, indice_key='subm3'),
            block(channels[2], channels[2], 3, norm_fn=norm_fn, padding=1, indice_key='subm3')
        )

        self.conv4 = spconv.SparseSequential(
            # [400, 352, 11] <- [200, 176, 5]
            block(channels[2], channels[2], 3, norm_fn=norm_fn, stride=2, padding=(0, 1, 1), indice_key='spconv4',
                  conv_type='spconv'),
            block(channels[2], channels[2], 3, norm_fn=norm_fn, padding=1, indice_key='subm4'),
            block(channels[2], channels[2], 3, norm_fn=norm_fn, padding=1, indice_key='subm4'),
            block(channels[2], channels[2], 3, norm_fn=norm_fn, padding=1, indice_key='subm4')
        )

        last_pad = 0
        last_pad = self.model_cfg.get('last_pad', last_pad)
        self.conv_out = spconv.SparseSequential(
            # [200, 150, 5] -> [200, 150, 2]
            spconv.SparseConv3d(channels[2], channels[3], (3, 1, 1), stride=(2, 1, 1), padding=last_pad,
                                bias=False, indice_key='spconv_down2'),
            build_norm_layer(norm_fn, channels[3])[1],
            nn.ReLU(),
        )
        self.num_point_features = channels[3]

    def forward(self, batch_dict):
        """
        Args:
            batch_dict:
                batch_size: int
                vfe_features: (num_voxels, C)
                voxel_coords: (num_voxels, 4), [batch_idx, z_idx, y_idx, x_idx]
        Returns:
            batch_dict:
                encoded_spconv_tensor: sparse tensor
        """
        voxel_features, voxel_coords = batch_dict['voxel_features'], batch_dict['voxel_coords']
        batch_size = batch_dict['batch_size']
        input_sp_tensor = spconv.SparseConvTensor(
            features=voxel_features,
            indices=voxel_coords.int(),
            spatial_shape=self.sparse_shape,
            batch_size=batch_size
        )

        x = self.conv_input(input_sp_tensor)

        x_conv1 = self.conv1(x)
        x_conv2 = self.conv2(x_conv1)
        x_conv3 = self.conv3(x_conv2)
        x_conv4 = self.conv4(x_conv3)

        # for detection head
        # [200, 176, 5] -> [200, 176, 2]
        out = self.conv_out(x_conv4)

        batch_dict.update({
            'encoded_spconv_tensor': out,
            'encoded_spconv_tensor_stride': 8
        })
        batch_dict.update({
            'multi_scale_3d_features': {
                # 'x_input': x,
                'x_conv1': x_conv1,
                'x_conv2': x_conv2,
                'x_conv3': x_conv3,
                'x_conv4': x_conv4,
            }
        })
        batch_dict.update({
            'multi_scale_3d_strides': {
                'x_conv1': 1,
                'x_conv2': 2,
                'x_conv3': 4,
                'x_conv4': 8,
            }
        })
        return batch_dict
