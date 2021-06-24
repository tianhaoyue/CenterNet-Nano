# config.py
import os.path


# centernet config
train_cfg = {
    'lr_epoch': (40, 60),
    'max_epoch': 70,
    'min_dim': [512, 512],
}
