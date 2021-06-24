import torch
import torch.nn as nn
import torch.nn.functional as F
from utils import Conv, DeConv, SPP, BottleneckCSP, SpatialPyramidPooling
from backbone import *
import numpy as np
import tools

import os
import matplotlib.pyplot as plt
import cv2
from backbone.mobilenet_v2 import mobilenet_v2
from backbone.mobilenet_v3 import mobilenet_v3
from backbone.shufflenetv2 import shufflenetv2
from backbone.ghostnet import ghostnet

class MobileNetV2(nn.Module):
    def __init__(self, pretrained = False):
        super(MobileNetV2, self).__init__()
        self.model = mobilenet_v2(pretrained=pretrained)

    def forward(self, x):
        #print((self.model.features[0]))
        out3 = self.model.features[:7](x)
        out4 = self.model.features[7:14](out3)
        out5 = self.model.features[14:18](out4)
        return out3, out4, out5

class MobileNetV3(nn.Module):
    def __init__(self, pretrained = False):
        super(MobileNetV3, self).__init__()
        self.model = mobilenet_v3(pretrained=pretrained)

    def forward(self, x):
        out3 = self.model.features[:7](x)
        out4 = self.model.features[7:13](out3)
        out5 = self.model.features[13:16](out4)
        return out3, out4, out5

class GhostNet(nn.Module):
    def __init__(self, pretrained=True):
        super(GhostNet, self).__init__()
        model = ghostnet()
        if pretrained:
            state_dict = torch.load("weights/ghostnet_1x.pth")
            model.load_state_dict(state_dict)
        del model.global_pool
        del model.conv_head
        del model.act2
        del model.classifier
        del model.blocks[9]
        self.model = model
        self.layers_out_filters = [16, 24, 40, 112, 160]

    def forward(self, x):
        x = self.model.conv_stem(x)
        x = self.model.bn1(x)
        x = self.model.act1(x)
        feature_maps = []

        for idx, block in enumerate(self.model.blocks):
            x = block(x)
            if idx in [2,4,6,8]:
                feature_maps.append(x)
        return feature_maps[1:]

class CenterNet(nn.Module):
    def __init__(self, device, input_size=None, trainable=False, num_classes=None, backbone='r50', conf_thresh=0.05, nms_thresh=0.45, topk=100, use_nms=False, hr=False):
        super(CenterNet, self).__init__()
        self.device = device
        self.input_size = input_size
        self.trainable = trainable
        self.num_classes = num_classes
        self.bk = backbone
        self.conf_thresh = conf_thresh
        self.nms_thresh = nms_thresh
        self.stride = 4
        self.topk = topk
        self.use_nms = use_nms
        self.grid_cell = self.create_grid(input_size)
        self.scale = np.array([[[input_size[0], input_size[1], input_size[0], input_size[1]]]])
        self.scale_torch = torch.tensor(self.scale.copy(), device=self.device).float()

        # backbone
        if self.bk == 'r18':
            print("Use backbone : resnet-18")
            self.backbone = resnet18(pretrained=trainable)
            c = 512
        elif self.bk == 'r34':
            print("Use backbone : resnet-34")
            self.backbone = resnet34(pretrained=trainable)
            c = 512
        elif self.bk == 'r50':
            print("Use backbone : resnet-50")
            self.backbone = resnet50(pretrained=trainable)
            c = 2048
        elif self.bk == 'r101':
            print("Use backbone : resnet-101")
            self.backbone = resnet101(pretrained=trainable)
            c = 2048
        elif self.bk == 'mbv2':
            print("Use backbone : mobilenetv2")
            self.backbone = MobileNetV2(pretrained=trainable)
            c = 320
        elif self.bk == 'mbv3':
            print("Use backbone : mobilenetv3")
            self.backbone = MobileNetV3(pretrained=trainable)
            c = 160
        elif self.bk == 'shuffv2':
            print("Use backbone : shufflenetv2")
            self.backbone = shufflenetv2(pretrained=trainable)
            c = 464
        elif self.bk == 'ghost':
            print("Use backbone : ghostnet")
            self.backbone = GhostNet(pretrained=trainable)
            c = 160
        else:
            print("Only support r18, r34, r50, r101 as backbone !!")
            exit()
            
        # # neck
        # self.spp = nn.Sequential(
        #     Conv(c, 256, k=1),
        #     SPP(),
        #     BottleneckCSP(256*4, c, n=1, shortcut=False)
        # )

        # #FPN
        # self.conv1x1_0 = Conv(368, 256, k=1)
        # self.conv1x1_1 = Conv(250, 96, k=1)
        # self.conv1x1_2 = Conv(250, 96, k=1)
        #
        # self.smooth_0 = Conv(96, 96, k=3, p=1)
        # self.smooth_1 = Conv(96, 96, k=3, p=1)
        # self.smooth_2 = Conv(96, 96, k=3, p=1)
        # self.smooth_3 = Conv(96, 96, k=3, p=1)

        # head
        #self.SPP = SpatialPyramidPooling()
        self.deconv5 = DeConv(c, 256, ksize=4, stride=2) # 32 -> 16
        self.deconv4 = DeConv(368, 256, ksize=4, stride=2) # 16 -> 8
        self.deconv3 = DeConv(296, 256, ksize=4, stride=2) #  8 -> 4

        self.cls_pred = nn.Sequential(
            Conv(256, 64, k=3, p=1),
            nn.Conv2d(64, self.num_classes, kernel_size=1)
        )

        self.txty_pred = nn.Sequential(
            Conv(256, 64, k=3, p=1),
            nn.Conv2d(64, 2, kernel_size=1)
        )
       
        self.twth_pred = nn.Sequential(
            Conv(256, 64, k=3, p=1),
            nn.Conv2d(64, 2, kernel_size=1)
        )


    def create_grid(self, input_size):
        h, w = input_size
        # generate grid cells
        ws, hs = w // self.stride, h // self.stride
        grid_y, grid_x = torch.meshgrid([torch.arange(hs), torch.arange(ws)])
        grid_xy = torch.stack([grid_x, grid_y], dim=-1).float()
        grid_xy = grid_xy.view(1, hs*ws, 2).to(self.device)
        
        return grid_xy


    def set_grid(self, input_size):
        self.grid_cell = self.create_grid(input_size)
        self.scale = np.array([[[input_size[0], input_size[1], input_size[0], input_size[1]]]])
        self.scale_torch = torch.tensor(self.scale.copy(), device=self.device).float()


    def decode_boxes(self, pred):
        """
        input box :  [delta_x, delta_y, sqrt(w), sqrt(h)]
        output box : [xmin, ymin, xmax, ymax]
        """
        output = torch.zeros_like(pred)
        pred[:, :, :2] = (torch.sigmoid(pred[:, :, :2]) + self.grid_cell) * self.stride
        pred[:, :, 2:] = (torch.exp(pred[:, :, 2:])) * self.stride

        # [c_x, c_y, w, h] -> [xmin, ymin, xmax, ymax]
        output[:, :, 0] = pred[:, :, 0] - pred[:, :, 2] / 2
        output[:, :, 1] = pred[:, :, 1] - pred[:, :, 3] / 2
        output[:, :, 2] = pred[:, :, 0] + pred[:, :, 2] / 2
        output[:, :, 3] = pred[:, :, 1] + pred[:, :, 3] / 2
        
        return output


    def _gather_feat(self, feat, ind, mask=None):
        dim  = feat.size(2)
        ind  = ind.unsqueeze(2).expand(ind.size(0), ind.size(1), dim)
        feat = feat.gather(1, ind)
        if mask is not None:
            mask = mask.unsqueeze(2).expand_as(feat)
            feat = feat[mask]
            feat = feat.view(-1, dim)
        return feat


    def _topk(self, scores):
        B, C, H, W = scores.size()
        
        topk_scores, topk_inds = torch.topk(scores.view(B, C, -1), self.topk)

        topk_inds = topk_inds % (H * W)
        
        topk_score, topk_ind = torch.topk(topk_scores.view(B, -1), self.topk)
        topk_clses = (topk_ind / self.topk).int()
        topk_inds = self._gather_feat(topk_inds.view(B, -1, 1), topk_ind).view(B, self.topk)

        return topk_score, topk_inds, topk_clses


    def nms(self, dets, scores):
        """"Pure Python NMS baseline."""
        x1 = dets[:, 0]  #xmin
        y1 = dets[:, 1]  #ymin
        x2 = dets[:, 2]  #xmax
        y2 = dets[:, 3]  #ymax

        areas = (x2 - x1) * (y2 - y1)                 # the size of bbox
        order = scores.argsort()[::-1]                        # sort bounding boxes by decreasing order

        keep = []                                             # store the final bounding boxes
        while order.size > 0:
            i = order[0]                                      #the index of the bbox with highest confidence
            keep.append(i)                                    #save it to keep
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])

            w = np.maximum(1e-28, xx2 - xx1)
            h = np.maximum(1e-28, yy2 - yy1)
            inter = w * h

            # Cross Area / (bbox + particular area - Cross Area)
            ovr = inter / (areas[i] + areas[order[1:]] - inter)
            #reserve all the boundingbox whose ovr less than thresh
            inds = np.where(ovr <= self.nms_thresh)[0]
            order = order[inds + 1]

        return keep


    def vis_fmap(self, fmap, sigmoid=False, name='p3'):
        """ fmap = [C, H, W] """
        save_path = os.path.join('vis_pred/' + name)
        os.makedirs(save_path, exist_ok=True)
        if sigmoid:
            f = torch.sigmoid(fmap)
        else:
            f = fmap
        
        f = torch.sum(fmap, dim=0)
        max_val = torch.max(f)
        f /= max_val
        f = f.cpu().numpy()
        f = cv2.resize(f, (self.input_size[1], self.input_size[0]), cv2.INTER_NEAREST)
        plt.imsave(os.path.join(save_path, name+'.jpg'), f)


    def forward(self, x, target=None):
        # backbone
        c3, c4, c5 = self.backbone(x)
        # mbv3
        # torch.Size([16, 40, 64, 64])
        # torch.Size([16, 112, 32, 32])
        # torch.Size([16, 160, 16, 16])
        B = c5.size(0)

        # p4 = self.conv1x1_1(c4)
        # p5 = self.conv1x1_2(c5)
        #
        # # FPN
        # p4 = self.smooth_0(p4 + F.interpolate(p5, scale_factor=2.0))
        # p3 = self.smooth_1(p3 + F.interpolate(p4, scale_factor=2.0))
        # bottom-up
        #c5 = self.spp(c5)
        p4 = self.deconv5(c5)
        p4 = torch.cat([p4,c4], dim=1)
        #p4 = self.conv1x1_0(p4)
        p3 = self.deconv4(p4)
        p3 = torch.cat([p3, c3], dim=1)
        p2 = self.deconv3(p3)

        # head
        cls_pred = self.cls_pred(p2)
        txty_pred = self.txty_pred(p2)
        twth_pred = self.twth_pred(p2)

        # train
        if self.trainable:
            # [B, H*W, num_classes]
            cls_pred = torch.sigmoid(cls_pred).permute(0, 2, 3, 1).contiguous().view(B, -1, self.num_classes)
            # [B, H*W, 2]
            txty_pred = txty_pred.permute(0, 2, 3, 1).contiguous().view(B, -1, 2)
            # [B, H*W, 2]
            twth_pred = twth_pred.permute(0, 2, 3, 1).contiguous().view(B, -1, 2)

            # compute loss
            cls_loss, txty_loss, twth_loss, total_loss = tools.loss(pred_cls=cls_pred, 
                                                                    pred_txty=txty_pred, 
                                                                    pred_twth=twth_pred, 
                                                                    label=target, 
                                                                    num_classes=self.num_classes
                                                                    )

            return cls_loss, txty_loss, twth_loss, total_loss       

        # test
        else:
            with torch.no_grad():
                # batch_size = 1
                cls_pred = torch.sigmoid(cls_pred)

                # visual class prediction
                self.vis_fmap(cls_pred[0], sigmoid=False, name='cls_pred')    

                # simple nms
                hmax = F.max_pool2d(cls_pred, kernel_size=5, padding=2, stride=1)
                keep = (hmax == cls_pred).float()
                cls_pred *= keep

                # decode box
                txtytwth_pred = torch.cat([txty_pred, twth_pred], dim=1).permute(0, 2, 3, 1).contiguous().view(B, -1, 4)
                # [B, H*W, 4] -> [H*W, 4]
                bbox_pred = torch.clamp((self.decode_boxes(txtytwth_pred) / self.scale_torch)[0], 0., 1.)

                # topk
                topk_scores, topk_inds, topk_clses = self._topk(cls_pred)

                topk_scores = topk_scores[0].cpu().numpy()
                topk_ind = topk_clses[0].cpu().numpy()
                topk_bbox_pred = bbox_pred[topk_inds[0]].cpu().numpy()

                if self.use_nms:
                    # nms
                    keep = np.zeros(len(topk_bbox_pred), dtype=np.int)
                    for i in range(self.num_classes):
                        inds = np.where(topk_ind == i)[0]
                        if len(inds) == 0:
                            continue
                        c_bboxes = topk_bbox_pred[inds]
                        c_scores = topk_scores[inds]
                        c_keep = self.nms(c_bboxes, c_scores)
                        keep[inds[c_keep]] = 1

                    keep = np.where(keep > 0)
                    topk_bbox_pred = topk_bbox_pred[keep]
                    topk_scores = topk_scores[keep]
                    topk_ind = topk_ind[keep]

                return topk_bbox_pred, topk_scores, topk_ind
                