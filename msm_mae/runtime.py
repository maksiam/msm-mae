"""MSM-MAE Runtime class/functions.
"""

# workaround for using heareval with `pip install -e .`
import sys
sys.path.append('..')

import logging
from pathlib import Path

import torch
import torch.nn as nn
from einops import rearrange
import nnAudio.Spectrogram

from . import models_mae


class Config:
    weight_file = '/path/to/80x96p16x16/checkpoint-99.pth' ## TO BE REPLACED WITH YOUR COPY.
    feature_d = 768
    fusion_layers = [] # [6,11]
    norm_type = all
    pooling_type = 'mean'

    model = 'mae_vit_base_patch16x16'
    input_size = [80, 208]
    patch_size = [16, 16]
    norm_pix_loss = False

    # FFT parameters.
    sample_rate = 16000
    n_fft = 400
    window_size = 400
    hop_size = 160
    n_mels = 80
    f_min = 50
    f_max = 8000
    window = 'hanning'


def parse_sizes_by_name(name):
    print(name)
    params = name.split('_')[0]
    params = params.split('p')
    input_str, patch_str = params[:2]
    input_size = [int(a) for a in input_str.split('x')]
    patch_size = [int(a) for a in patch_str.split('x')]
    model_option = '' if len(params) < 3 else params[2]
    return input_size, patch_size, model_option
    # parse_sizes_by_name('80x208p16x16') --> ([80, 208], [16, 16])


def get_model(args, weight_file):
    folder_name = Path(weight_file).parent.name
    args.input_size, args.patch_size, model_option = parse_sizes_by_name(folder_name)

    checkpoint = torch.load(weight_file, map_location='cpu')
    checkpoint = checkpoint['model'] if 'model' in checkpoint else checkpoint
    args.model = f'mae_vit_base_patch{args.patch_size[0]}x{args.patch_size[1]}'
    logging.info(f'Creating model: {args.model}')
    model = models_mae.__dict__[args.model](img_size=args.input_size, norm_pix_loss=args.norm_pix_loss,
        use_cls_token=('cls_token' in checkpoint))
    model.load_state_dict(checkpoint)
    model.eval()

    return model


def get_to_melspec(cfg):
    to_spec = nnAudio.Spectrogram.MelSpectrogram(
        sr=cfg.sample_rate,
        n_fft=cfg.n_fft,
        win_length=cfg.window_size,
        hop_length=cfg.hop_size,
        n_mels=cfg.n_mels,
        fmin=cfg.f_min,
        fmax=cfg.f_max,
        center=True,
        power=2,
        verbose=False,
    )
    print(f'Runtime MelSpectrogram({cfg.sample_rate}, {cfg.n_fft}, {cfg.window_size}, {cfg.hop_size}, '
        + f'{cfg.n_mels}, {cfg.f_min}, {cfg.f_max}):')
    print(to_spec)
    return to_spec


def get_timestamps(cfg, batch_audio, x): # Returns timestamps in milliseconds.
    audio_len = len(batch_audio[0])
    sec = audio_len / cfg.sample_rate
    x_len = len(x[0])
    step = sec / x_len * 1000 # sec -> ms
    ts = torch.tensor([step * i for i in range(x_len)]).unsqueeze(0)
    ts = ts.repeat(len(batch_audio), 1)
    return ts


class RuntimeMAE(nn.Module):
    def __init__(self, cfg=Config(), weight_file=None):
        super().__init__()
        cfg.weight_file = weight_file or cfg.weight_file

        self.cfg = cfg
        self.backbone = get_model(cfg, cfg.weight_file)
        logging.info(str(cfg))
        logging.info(f'Model input size: {cfg.input_size}')
        logging.info(f'Using weights: {cfg.weight_file}')
        logging.info(f'Has [CLS] token?: {self.backbone.use_cls_token}')

        self.to_spec = get_to_melspec(cfg)

        self.sample_rate = cfg.sample_rate

    def to_feature(self, batch_audio):
        # raw -> spectrogram, and normalize
        x = self.to_spec(batch_audio)
        x = (x + torch.finfo().eps).log()
        x = x.unsqueeze(1)
        return x

    def normalize_batch(self, x):
        mu, sigma = x.mean(), x.std()
        x = (x - mu) / sigma
        return x

    def to_normalized_spec(self, batch_audio):
        # raw -> spectrogram
        x = self.to_feature(batch_audio)
        # normalize among batch samples
        x = self.normalize_batch(x)
        return x

    def encode_lms(self, lms):
        x = lms

        patch_fbins = self.backbone.grid_size()[0]
        unit_frames = self.cfg.input_size[1]
        embed_d = self.backbone.patch_embed.proj.out_channels
        cur_frames = x.shape[-1]
        pad_frames = unit_frames - (cur_frames % unit_frames)
        if pad_frames > 0:
            x = torch.nn.functional.pad(x, (0, pad_frames))

        embeddings = []
        if True:
            # stack embeddings
            for i in range(x.shape[-1] // unit_frames):
                emb, _, _ = self.backbone.forward_encoder(x[..., i*unit_frames:(i+1)*unit_frames], mask_ratio=0.)
                if self.backbone.use_cls_token:
                    emb = emb[:, 1:, :]
                emb = rearrange(emb, 'b (f t) d -> b t (f d)', f=patch_fbins, d=embed_d)
                embeddings.append(emb)
        elif False:
            # CLS only
            for i in range(x.shape[-1] // unit_frames):
                emb, _, _ = self.backbone.forward_encoder(x[..., i*unit_frames:(i+1)*unit_frames], mask_ratio=0.)
                assert self.backbone.use_cls_token, '[CLS] NOT AVAILABLE'
                emb = emb[:, :1, :]
                # emb = rearrange(emb, 'b (f t) d -> b t (f d)', f=patch_fbins, d=embed_d)
                embeddings.append(emb)
        else:
            # mean all
            for i in range(x.shape[-1] // unit_frames):
                emb, _, _ = self.backbone.forward_encoder(x[..., i*unit_frames:(i+1)*unit_frames], mask_ratio=0.)
                if self.backbone.use_cls_token:
                    emb = emb[:, 1:, :]
                embeddings.append(emb)
        x = torch.hstack(embeddings)
        pad_emb_frames = int(embeddings[0].shape[1] * pad_frames / unit_frames)
        # print(2, x.shape, embeddings[0].shape, pad_emb_frames)
        if pad_emb_frames > 0:
            x = x[:, :-pad_emb_frames] # remove padded tail
        # print(3, x.shape)
        return x

    def encode(self, batch_audio):
        x = self.to_normalized_spec(batch_audio)
        return self.encode_lms(x)

    def get_scene_embeddings(self, audio):
        """
        audio: n_sounds x n_samples of mono audio in the range [-1, 1]. All sounds in a batch will be padded/trimmed to the same length.
        Returns:
            embedding: A float32 Tensor with shape (n_sounds, model.scene_embedding_size).
        """
        x = self.encode(audio)
        x = torch.mean(x, dim=1)
        return x

    def get_timestamp_embeddings(self, audio):
        """
        audio: n_sounds x n_samples of mono audio in the range [-1, 1]. All sounds in a batch will be padded/trimmed to the same length.
        Returns:
            embedding: A float32 Tensor with shape (n_sounds, n_timestamps, model.timestamp_embedding_size).
            timestamps: A float32 Tensor with shape (`n_sounds, n_timestamps). Centered timestamps in milliseconds corresponding to each embedding in the output.
        """
        x = self.encode(audio)
        ts = get_timestamps(self.cfg, audio, x)
        print(audio.shape, x.shape, ts.shape)
        return x, ts


    def get_basic_timestamp_embeddings(self, audio):
        """
        audio: n_sounds x n_samples of mono audio in the range [-1, 1]. All sounds in a batch will be padded/trimmed to the same length.
        Returns:
            embedding: A float32 Tensor with shape (n_sounds, n_timestamps, model.timestamp_embedding_size).
            timestamps: A float32 Tensor with shape (`n_sounds, n_timestamps). Centered timestamps in milliseconds corresponding to each embedding in the output.
        """
        assert False, 'return get_basic_timestamp_embeddings(audio, model)'

