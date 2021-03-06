import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models


class Model(nn.Module):
    def __init__(self, arch, pretrained=False, nclass=None):
        super(Model, self).__init__()
        model = torchvision.models.__dict__[arch](pretrained)
        if arch.startswith('alexnet') or arch.startswith('vgg'):
            model.features = torch.nn.DataParallel(model.features)
        if nclass is not None and nclass != model.module.fc.out_features:
            if arch.startswith('resnet'):
                model.module.fc = nn.Linear(model.module.fc.in_features,
                                            nclass)
            else:
                raise Exception('Not implemented.')
        self.model = model

    def forward(self, x):
        out = self.model(x)
        return F.log_softmax(out, dim=-1)
