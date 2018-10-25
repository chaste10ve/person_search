# -----------------------------------------------------
# Spatial Invariant Person Search Network
#
# Author: Liangqi Li and Xinlei Chen
# Creating Date: Apr 1, 2018
# Latest rectified: Oct 25, 2018
# -----------------------------------------------------
import torch
import torch.nn as nn
import torch.nn.functional as func
import yaml

from .vgg16 import Vgg16
from .resnet import MyResNet
from .densenet import DenseNet
from .strpn import STRPN
from utils.losses import oim_loss, smooth_l1_loss


class SIPN(nn.Module):

    def __init__(self, net_name, dataset_name, pre_model=None, is_train=True):
        super().__init__()
        self.net_name = net_name
        self.is_train = is_train

        if dataset_name == 'sysu':
            self.num_pid = 5532
            self.queue_size = 5000
        elif dataset_name == 'prw':
            self.num_pid = 483
            self.queue_size = 500
        else:
            raise KeyError(dataset_name)
        self.lut_momentum = 0.5
        self.reid_feat_dim = 256

        self.register_buffer('lut', torch.zeros(
            self.num_pid, self.reid_feat_dim).cuda())
        self.register_buffer('queue', torch.zeros(
            self.queue_size, self.reid_feat_dim).cuda())

        if self.net_name == 'vgg16':
            self.net = Vgg16(pre_model, self.is_train)
        elif self.net_name == 'res34':
            self.net = MyResNet(34, pre_model, self.is_train)
        elif self.net_name == 'res50':
            self.net = MyResNet(50, pre_model, self.is_train)
        elif self.net_name == 'dense121':
            self.net = DenseNet(121, pre_model, self.is_train)
        elif self.net_name == 'dense161':
            self.net = DenseNet(161, pre_model, self.is_train)
        else:
            raise KeyError(self.net_name)

        self.fc7_channels = self.net.fc7_channels

        # SPIN consists of three main parts
        self.head = self.net.head
        self.strpn = STRPN(self.net.net_conv_channels, self.num_pid,
                           self.is_train)
        self.tail = self.net.tail

        self.cls_score_net = nn.Linear(self.fc7_channels, 2)
        self.bbox_pred_net = nn.Linear(self.fc7_channels, 8)
        self.reid_feat_net = nn.Linear(self.fc7_channels, self.reid_feat_dim)
        self.init_linear_weight(False)

    def forward(self, im_data, gt_boxes, im_info, mode='gallery'):
        if self.is_train:
            net_conv = self.head(im_data)
            # returned parameters contain 3 tuples here
            pooled_feat, trans_feat, rpn_loss, label, bbox_info = self.strpn(
                net_conv, gt_boxes, im_info)
            if self.net_name == 'vgg16':
                pooled_feat = pooled_feat.view(pooled_feat.size(0), -1)
                fc7 = self.tail(pooled_feat)
            else:
                fc7 = self.tail(pooled_feat).mean(3).mean(2)
            cls_score = self.cls_score_net(fc7)
            bbox_pred = self.bbox_pred_net(fc7)

            # reid_fc7 = self.tail(trans_feat).mean(3).mean(2)
            # reid_feat = F.normalize(self.reid_feat_net(reid_fc7))
            reid_feat = func.normalize(self.reid_feat_net(fc7))

            det_label, pid_label = label
            det_label = det_label.view(-1)
            cls_loss = func.cross_entropy(cls_score.view(-1, 2), det_label)
            bbox_loss = smooth_l1_loss(bbox_pred, bbox_info)
            reid_loss = oim_loss(reid_feat, pid_label, self.lut, self.queue,
                                 gt_boxes.size(0), self.lut_momentum)
            rpn_cls_loss, rpn_box_loss = rpn_loss

            return rpn_cls_loss, rpn_box_loss, cls_loss, bbox_loss, reid_loss

        else:
            if mode == 'gallery':
                net_conv = self.head(im_data)
                rois, pooled_feat, trans_feat = self.strpn(
                    net_conv, gt_boxes, im_info)
                if self.net_name == 'vgg16':
                    pooled_feat = pooled_feat.view(pooled_feat.size(0), -1)
                    fc7 = self.tail(pooled_feat)
                else:
                    fc7 = self.tail(pooled_feat).mean(3).mean(2)
                cls_score = self.cls_score_net(fc7)
                bbox_pred = self.bbox_pred_net(fc7)

                # reid_fc7 = self.tail(trans_feat).mean(3).mean(2)
                # reid_feat = F.normalize(self.reid_feat_net(reid_fc7))
                reid_feat = func.normalize(self.reid_feat_net(fc7))

                cls_prob = func.softmax(cls_score, 1)

                with open('config.yml', 'r') as f:
                    config = yaml.load(f)
                mean = config['train_bbox_normalize_means']
                std = config['train_bbox_normalize_stds']
                means = bbox_pred.new(mean).repeat(2).unsqueeze(0).expand_as(
                    bbox_pred)
                stds = bbox_pred.new(std).repeat(2).unsqueeze(0).expand_as(
                    bbox_pred)
                bbox_pred = bbox_pred.mul(stds).add(means)

                cls_prob = cls_prob.cpu().numpy()
                bbox_pred = bbox_pred.cpu().numpy()
                rois = rois.cpu().numpy()
                reid_feat = reid_feat.cpu().numpy()

                return cls_prob, bbox_pred, rois, reid_feat

            elif mode == 'query':
                net_conv = self.head(im_data)
                # TODO: move pooling layer from strpn to SIPN
                pooled_feat = self.strpn(net_conv, gt_boxes, im_info, mode)
                if self.net_name == 'vgg16':
                    pooled_feat = pooled_feat.view(pooled_feat.size(0), -1)
                    fc7 = self.tail(pooled_feat)
                else:
                    fc7 = self.tail(pooled_feat).mean(3).mean(2)
                reid_feat = func.normalize(self.reid_feat_net(fc7))

                return reid_feat.data.cpu().numpy()

            else:
                raise KeyError(mode)

    def train(self, mode=True):
        nn.Module.train(self, mode)
        self.net.train(mode)

    def init_linear_weight(self, trun):
        def normal_init(m, mean, stddev, truncated=False):
            """
            weight initializer: truncated normal and random normal.
            """
            # x is a parameter
            if truncated:
                m.weight.data.normal_().fmod_(2).mul_(stddev).add_(
                    mean)  # not a perfect approximation
            else:
                m.weight.data.normal_(mean, stddev)
            m.bias.data.zero_()

        normal_init(self.cls_score_net, 0, 0.01, trun)
        normal_init(self.bbox_pred_net, 0, 0.001, trun)
        # TODO: change 0.01 for reid_feat_net
        normal_init(self.reid_feat_net, 0, 0.01, trun)

    def load_trained_model(self, state_dict):
        nn.Module.load_state_dict(
            self, {k: state_dict[k] for k in list(self.state_dict())})