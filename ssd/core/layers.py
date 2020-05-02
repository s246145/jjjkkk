from torch import nn
import torch
from torch.nn import init
from torch.nn import functional as F
import numpy as np

class Flatten(nn.Module):
    def forward(self, x):
        batch_size = x.shape[0]
        return x.view(batch_size, -1)


class L2Normalization(nn.Module):
    def __init__(self, channels, gamma=20):
        super().__init__()
        self.gamma = gamma
        self.in_channels = channels
        self.out_channels = channels
        self.scales = nn.Parameter(torch.Tensor(self.in_channels)) # trainable
        self.reset_parameters()

    def reset_parameters(self):
        init.constant_(self.scales, self.gamma) # initialized with gamma first

    # Note that pytorch's dimension order is batch_size, channels, height, width
    def forward(self, x):
        # |x|_2
        # normalize (x^)
        x = F.normalize(x, p=2, dim=1)
        return self.scales.unsqueeze(0).unsqueeze(2).unsqueeze(3) * x


class Predictor(nn.Module):
    def __init__(self, total_dbox_nums, class_nums):
        super().__init__()

        self._total_dbox_nums = total_dbox_nums
        self._class_nums = class_nums

    def forward(self, locs, confs):
        """
        :param locs: list of Tensor, Tensor's shape is (batch, c, h, w)
        :param confs: list of Tensor, Tensor's shape is (batch, c, h, w)
        :return: predicts: localization and confidence Tensor, shape is (batch, total_dbox_num * (4+class_nums))
        """
        locs_reshaped, confs_reshaped = [], []
        for loc, conf in zip(locs, confs):
            batch_num = loc.shape[0]

            # original feature => (batch, (class_num or 4)*dboxnum, fmap_h, fmap_w)
            # converted into (batch, fmap_h, fmap_w, (class_num or 4)*dboxnum)
            # contiguous means aligning stored 1-d memory for given array
            loc = loc.permute((0, 2, 3, 1)).contiguous()
            locs_reshaped += [loc.reshape((batch_num, -1))]

            conf = conf.permute((0, 2, 3, 1)).contiguous()
            confs_reshaped += [conf.reshape((batch_num, -1))]



        locs_reshaped = torch.cat(locs_reshaped, dim=1).reshape((-1, self._total_dbox_nums, 4))
        confs_reshaped = torch.cat(confs_reshaped, dim=1).reshape((-1, self._total_dbox_nums, self._class_nums))

        return torch.cat((locs_reshaped, confs_reshaped), dim=2)

class Conv2d:
    batch_norm = True

    @staticmethod
    def relu_block(order, block_num, in_channels, out_channels, **kwargs):
        """
        :param order: int or str
        :param block_num: int, how many conv layers are sequenced
            NOTE: layer's name *{order}_{number in relu_block}. * represents layer name.
        :param in_channels: int
        :param out_channels: int
        :param kwargs: key lists are below;
                Conv2d params:
                    conv_k_size: int or tuple, conv2d layer's kernel size. Default is (3, 3)
                    conv_k_stride: int or tuple, conv2d layer's stride. Default is (1, 1)
                    conv_padding: int or tuple, Zero-padding added to both sides of the input. Default is 1

                BatcnNorm2d param:
                    batch_norm: bool, whether to add batch normalization layer. Default is Conv2d.batch_norm

                ReLu param:
                    relu_inplace: bool, whether to inplace in relu

                Maxpool2d params:
                    pool_k_size: int or tuple, maxpool2d layer's kernel size. Default is (2, 2)
                    pool_stride: int or tuple, maxpool2d layer's stride. Default is (2, 2)
                    pool_ceil_mode: bool, whether to ceil in pooling
                    pool_padding: int or tuple, implicit zero padding to be added on both sides. Default is 0

        :return: list of tuple is for OrderedDict
        """
        kernel_size = kwargs.pop('conv_k_size', (3, 3))
        stride = kwargs.pop('conv_stride', (1, 1))
        padding = kwargs.pop('conv_padding', 1)
        relu_inplace = kwargs.pop('relu_inplace', False)# TODO relu inplace problem >>conv4
        batch_norm = kwargs.pop('batch_norm', Conv2d.batch_norm)

        in_c = in_channels
        layers = []
        # append conv relu_block
        for bnum in range(block_num):
            postfix = '{0}_{1}'.format(order, bnum + 1)
            if not batch_norm:
                layers += [
                    ('conv{}'.format(postfix),
                     nn.Conv2d(in_c, out_channels, kernel_size, stride=stride, padding=padding)),
                    ('relu{}'.format(postfix), nn.ReLU(relu_inplace))
                ]
            else:
                layers += [
                    ('conv{}'.format(postfix),
                     nn.Conv2d(in_c, out_channels, kernel_size, stride=stride, padding=padding)),
                    ('bn{}'.format(postfix), nn.BatchNorm2d(out_channels)),
                    ('relu{}'.format(postfix), nn.ReLU(relu_inplace))
                ]
            in_c = out_channels

        kernel_size = kwargs.pop('pool_k_size', (2, 2))
        stride = kwargs.pop('pool_stride', (2, 2))
        ceil_mode = kwargs.pop('pool_ceil_mode', False)
        padding = kwargs.pop('pool_padding', 0)
        # append maxpooling
        layers += [
            ('pool{}'.format(order), nn.MaxPool2d(kernel_size, stride=stride, ceil_mode=ceil_mode, padding=padding))
        ]

        return layers

    @staticmethod
    def block(order, block_num, in_channels, out_channels, **kwargs):
        """
        :param order: int or str
        :param block_num: int, how many conv layers are sequenced
            NOTE: layer's name *{order}_{number in relu_block}. * represents layer name.
        :param in_channels: int or tuple
        :param out_channels: int or tuple
        :param kwargs:
                Conv2d params:
                    conv2d layer's kwargs. See nn.Conv2d
                BatcnNorm2d param:
                    batch_norm: bool, whether to add batch normalization layer. Default is Conv2d.batch_norm

        :return:
        """
        batch_norm = kwargs.pop('batch_norm', Conv2d.batch_norm)

        if isinstance(out_channels, int):
            out_channels = tuple(out_channels for _ in range(block_num))
        if isinstance(in_channels, int):
            in_channels = [in_channels]
            for out_c in out_channels[:-1]:
                in_channels += [out_c]
            in_channels = tuple(in_channels)

        if not (len(out_channels) == block_num and len(in_channels) == len(out_channels)):
            raise ValueError('block_nums and length of out_channels and in_channels must be same')

        layers = []
        # append conv relu_block
        for bnum, (in_c, out_c) in enumerate(zip(in_channels, out_channels)):
            postfix = '{0}_{1}'.format(order, bnum + 1)
            if not batch_norm:
                layers += [
                    ('conv{}'.format(postfix),
                     nn.Conv2d(in_c, out_c, **kwargs))
                ]
            else:
                layers += [
                    ('conv{}'.format(postfix),
                     nn.Conv2d(in_c, out_c, **kwargs)),
                    ('bn{}'.format(postfix), nn.BatchNorm2d(out_c))
                ]

        return layers

    @staticmethod
    def relu_one(postfix, in_channels, out_channels, relu_inplace=False, **kwargs):
        batch_norm = kwargs.pop('batch_norm', Conv2d.batch_norm)
        if not batch_norm:
            return [
                ('conv{}'.format(postfix), nn.Conv2d(in_channels, out_channels, **kwargs)),
                ('relu{}'.format(postfix), nn.ReLU(inplace=relu_inplace))
            ]
        else:
            return [
                ('conv{}'.format(postfix), nn.Conv2d(in_channels, out_channels, **kwargs)),
                ('bn{}'.format(postfix), nn.BatchNorm2d(out_channels)),
                ('relu{}'.format(postfix), nn.ReLU(inplace=relu_inplace))
            ]

    @staticmethod
    def one(postfix, in_channels, out_channels, **kwargs):
        batch_norm = kwargs.pop('batch_norm', Conv2d.batch_norm)
        if not batch_norm:
            return [
                ('conv{}'.format(postfix), nn.Conv2d(in_channels, out_channels, **kwargs))
            ]
        else:
            return [
                ('conv{}'.format(postfix), nn.Conv2d(in_channels, out_channels, **kwargs)),
                ('bn{}'.format(postfix), nn.BatchNorm2d(out_channels))
            ]