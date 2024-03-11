import os
import random
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

import librosa
import numpy as np
import pandas as pd
import torch
import torchaudio.transforms as a_T
from audiomentations import AddGaussianNoise, Compose, PitchShift, Shift, TimeStretch
from python_speech_features import logfbank
from pytorch_lightning import LightningDataModule
from pytorch_lightning.utilities.types import EVAL_DATALOADERS, TRAIN_DATALOADERS
from scipy.io import wavfile
from torch import Tensor
from torch.nn import functional as F
from torch.utils.data import DataLoader, Dataset, RandomSampler
from torch.utils.data.sampler import WeightedRandomSampler

import models.avhubert.utils as custom_utils


@dataclass
class Metadata:
    source: str
    target1: str
    target1: str
    method: str
    category: str
    type: str
    race: str
    gender: str
    vid: str
    path: str


dtype = {
    "source": str,
    "target1": str,
    "target2": str,
    "method": str,
    "category": str,
    "type": str,
    "race": str,
    "gender": str,
    "vid": str,
    "path": str,
}

T_LABEL = Union[Tensor, Tuple[Tensor, Tensor, Tensor]]


def seed_worker(worker_id):
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)


g = torch.Generator()
g.manual_seed(0)


class Fakeavceleb(Dataset):

    def __init__(
        self,
        subset: str,
        root: str = "data",
        metadata: Optional[List[Metadata]] = None,
        augmentation: bool = False,
    ):
        self.subset = subset
        self.root = root

        self.image_size = 128
        self.image_crop_size = 540
        self.image_mean = 0.421
        self.image_std = 0.165
        self.stack_order_audio = 4
        self.pad_audio = False
        self.scale_percent = 0.5
        self.augmenation = augmentation
        self.audio_augment = Compose(
            [
                # TimeStretch(min_rate=0.8, max_rate=1.25, p=0.5, leave_length_unchanged=True),
                Shift(min_shift=-0.1, max_shift=0.1, rollover=True, p=1),
            ]
        )
        print(f"augmentation: {self.augmenation}")

        self.metadata = metadata

        if self.subset in "train":
            self.transform = custom_utils.Compose(
                [
                    custom_utils.Normalize(0.0, 255.0),
                    custom_utils.RandomCrop((100, 100)),
                    custom_utils.HorizontalFlip(0.5),
                    custom_utils.Normalize(self.image_mean, self.image_std),
                ]
            )

        else:
            self.transform = custom_utils.Compose(
                [
                    custom_utils.Normalize(0.0, 255.0),
                    custom_utils.CenterCrop((100, 100)),
                    custom_utils.Normalize(self.image_mean, self.image_std),
                ]
            )

    def __getitem__(self, index: int) -> List[Tensor]:
        meta = self.metadata.iloc[index]

        c_label = 1 if ("RealVideo" in meta.type) and ("RealAudio" in meta.type) else 0
        v_label = 1 if "RealVideo" in meta.type else 0
        a_label = 1 if "RealAudio" in meta.type else 0
        m_label = 1 if "real" in meta.method else 0

        if "RealVideo-RealAudio" in meta.type:
            mm_label = 0
        elif "FakeVideo-RealAudio" in meta.type:
            mm_label = 1
        elif "RealVideo-FakeAudio" in meta.type:
            mm_label = 2
        else:
            mm_label = 3
        if meta.category == "B":
            s_label = 0  # real_video-fake_audio
        else:
            s_label = 1
        path = "/".join(meta.path.split("/")[1:])
        file_path = os.path.join(self.root, path, meta.vid)
        video, audio, modified = self.load_feature(file_path)

        if modified:
            s_label = 0

        audio, video = torch.from_numpy(audio.astype(np.float32)) if audio is not None else None, (
            torch.from_numpy(video.astype(np.float32)) if video is not None else None
        )
        if audio is not None:
            with torch.no_grad():
                audio = F.layer_norm(audio, audio.shape[1:])

        return {
            "id": index,
            "file": os.path.join(meta.path, meta.vid),
            "video": video,
            "audio": audio,
            "v_label": torch.tensor(v_label),
            "a_label": torch.tensor(a_label),
            "c_label": torch.tensor(c_label),
            "m_label": torch.tensor(m_label),
            "mm_label": torch.tensor(mm_label),
            "s_label": torch.tensor(s_label),
        }

    def __len__(self) -> int:
        # print(self.subset, self.metadata.shape[0])
        return self.metadata.shape[0]

    def load_video(self, video_name):
        feats = custom_utils.load_video(video_name, self.scale_percent)
        feats = self.transform(feats)
        # feats = np.expand_dims(feats, axis=-1)
        return feats

    def load_feature(self, mix_name):
        """
        Load image and audio feature
        Returns:
        video_feats: numpy.ndarray of shape [T, H, W, 1], audio_feats: numpy.ndarray of shape [T, F]
        """

        def stacker(feats, stack_order):
            """
            Concatenating consecutive audio frames
            Args:
            feats - numpy.ndarray of shape [T, F]
            stack_order - int (number of neighboring frames to concatenate
            Returns:
            feats - numpy.ndarray of shape [T', F']
            """
            feat_dim = feats.shape[1]
            if len(feats) % stack_order != 0:
                res = stack_order - len(feats) % stack_order
                res = np.zeros([res, feat_dim]).astype(feats.dtype)
                feats = np.concatenate([feats, res], axis=0)
            feats = feats.reshape((-1, stack_order, feat_dim)).reshape(-1, stack_order * feat_dim)
            return feats

        video_fn = mix_name
        modified = False
        # video_fn = video_fn.replace(" (", "-")
        # video_fn = video_fn.replace(")", "")
        audio_fn = video_fn.replace(".mp4", ".wav")

        video_feats = self.load_video(video_fn)  # [T, H, W, 1]

        sample_rate, wav_data = wavfile.read(audio_fn)
        wav_data = wav_data[:, 0]
        wav_data = wav_data.astype(np.float32)

        if self.augmenation and self.subset in "train":
            p = 0.5
            if random.random() < p:
                wav_data = self.audio_augment(samples=wav_data, sample_rate=16000)
                modified = True

        assert sample_rate == 16_000 and len(wav_data.shape) == 1
        audio_feats = logfbank(wav_data, samplerate=sample_rate).astype(np.float32)  # [T, F]

        audio_feats = stacker(
            audio_feats, self.stack_order_audio
        )  # [T/stack_order_audio, F*stack_order_audio]

        if audio_feats is not None and video_feats is not None:
            diff = len(audio_feats) - len(video_feats)
            if diff < 0:
                audio_feats = np.concatenate(
                    [audio_feats, np.zeros([-diff, audio_feats.shape[-1]], dtype=audio_feats.dtype)]
                )
            elif diff > 0:
                audio_feats = audio_feats[:-diff]

        return video_feats, audio_feats, modified


class FakeavcelebDataModule(LightningDataModule):
    train_dataset: Fakeavceleb
    dev_dataset: Fakeavceleb
    test_dataset: Fakeavceleb
    metadata: List[Metadata]

    def __init__(
        self,
        root: str = "/data/kyungbok/FakeAVCeleb_v1.2",
        train_fold: str = None,
        batch_size: int = 1,
        num_workers: int = 0,
        max_sample_size=500,
        take_train: int = None,
        take_dev: int = None,
        take_test: int = None,
        augmentation: bool = False,
        dataset_type: str = "original",
    ):
        print("batch_size", batch_size)
        print("num_workers", num_workers)
        super().__init__()
        self.root = root
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.take_train = take_train
        self.take_dev = take_dev
        self.take_test = take_test
        self.Dataset = Fakeavceleb
        self.pad_audio = False
        self.random_crop = False
        self.max_sample_size = max_sample_size
        self.train_fold = train_fold
        self.augmentation = augmentation
        self.dataset_type = dataset_type

    def setup(self, stage: Optional[str] = None) -> None:
        self.metadata = pd.read_csv(os.path.join(self.root, "meta_data.csv"), dtype=dtype)  # .loc
        print(self.root, self.train_fold)
        self.metadata.columns = dtype.keys()
        if self.dataset_type == "original":
            self.set_original_dataset()
        elif self.dataset_type == "new":
            self.set_new_dataset()

    def set_new_dataset(self):
        print("new dataset")
        df = self.metadata

        train_df_A = df[df["category"] == "A"].sample(350, random_state=42)
        train_df_B = df[df["category"] == "B"].sample(350, random_state=42)
        train_df_C = (
            df[(df["category"] == "C") & (df["method"] != "faceswap")]
            .groupby("method")
            .sample(n=175, random_state=42)
        )
        train_df_D = (
            df[(df["category"] == "D") & (df["method"] != "faceswap-wav2lip")]
            .groupby("method")
            .sample(n=175, random_state=42)
        )
        self.train_metadata = pd.concat([train_df_A, train_df_B, train_df_C, train_df_D])

        val_df_A = df[df["category"] == "A"].drop(train_df_A.index)[:50]

        val_df_B = df[df["category"] == "B"].drop(train_df_B.index)[:50]
        val_df_C = (
            df[(df["category"] == "C") & (df["method"] != "faceswap")]
            .drop(train_df_C.index)
            .groupby("method")
            .sample(n=25, random_state=42)
        )
        val_df_D = (
            df[(df["category"] == "D") & (df["method"] != "faceswap-wav2lip")]
            .drop(train_df_D.index)
            .groupby("method")
            .sample(n=25, random_state=42)
        )

        self.val_metadata = pd.concat([val_df_A, val_df_B, val_df_C, val_df_D])

        test_df_A = df[df["category"] == "A"].drop(train_df_A.index).drop(val_df_A.index)
        # test_df_A = df[df["category"] == "A"].drop(train_df_A.index)
        # test_df_B = df[df["category"] == "B"].drop(train_df_B.index)
        test_df_C = df[(df["category"] == "C") & (df["method"] == "faceswap")].sample(n=100, random_state=42)
        test_df_D = df[(df["category"] == "D") & (df["method"] == "faceswap-wav2lip")].sample(
            n=100, random_state=42
        )
        self.test_metadata = pd.concat([test_df_A, test_df_C, test_df_D])

        print(len(self.train_metadata), len(self.val_metadata), len(self.test_metadata))

        self.train_dataset = self.Dataset(
            "train", self.root, metadata=self.train_metadata, augmentation=self.augmentation
        )

        self.val_dataset = self.Dataset(
            "val", self.root, metadata=self.val_metadata, augmentation=self.augmentation
        )
        self.test_dataset = self.Dataset(
            "test", self.root, metadata=self.test_metadata, augmentation=self.augmentation
        )

    def set_original_dataset(self):
        self.train_fold = os.path.join(self.root, self.train_fold)

        print(self.train_fold)

        with open(self.train_fold) as train_f:
            train_ids = []
            for train_line in train_f:
                train_id = train_line[:7]
                if "id" in train_id:
                    train_ids.append(train_id)

        train_metadata = self.metadata[self.metadata["source"].isin(train_ids)]
        test_metadata = self.metadata[~self.metadata["source"].isin(train_ids)]

        train_metadata_A = train_metadata[train_metadata["category"].isin(["A"])][:400]
        train_metadata_B = train_metadata[train_metadata["category"].isin(["B"])][:400]
        train_metadata_C = train_metadata[train_metadata["category"].isin(["C"])][:400]
        train_metadata_D = train_metadata[train_metadata["category"].isin(["D"])][:400]

        self.train_metadata = pd.concat(
            [train_metadata_A, train_metadata_B, train_metadata_C, train_metadata_D]
        )
        # self.train_metadata = train_metadata
        #
        test_metadata_A = test_metadata[test_metadata["category"].isin(["A"])][:100]
        test_metadata_B = test_metadata[test_metadata["category"].isin(["B"])][:100]
        test_metadata_C = test_metadata[test_metadata["category"].isin(["C"])][:100]
        test_metadata_D = test_metadata[test_metadata["category"].isin(["D"])][:100]
        #
        self.test_metadata = pd.concat([test_metadata_A, test_metadata_B, test_metadata_C, test_metadata_D])
        # self.test_metadata = test_metadata

        self.train_dataset = self.Dataset(
            "train", self.root, metadata=self.train_metadata, augmentation=self.augmentation
        )
        self.test_dataset = self.Dataset(
            "test", self.root, metadata=self.test_metadata, augmentation=self.augmentation
        )

    def crop_to_max_size(self, wav, target_size, start=None):
        size = len(wav)
        diff = size - target_size
        if diff <= 0:
            return wav, 0
        # longer utterances
        if start is None:
            start, end = 0, target_size
            if self.random_crop:
                start = np.random.randint(0, diff + 1)
                end = size - diff + start
        else:
            end = start + target_size
        return wav[start:end], start

    def collater(self, samples):
        samples = [s for s in samples if s["id"] is not None]
        if len(samples) == 0:
            return {}

        audio, video = [s["audio"] for s in samples], [s["video"] for s in samples]
        if audio[0] is None:
            audio = None
        if video[0] is None:
            video = None
        if audio is not None:
            audio_sizes = [len(s) for s in audio]
        else:
            audio_sizes = [len(s) for s in video]
        if self.pad_audio:
            audio_size = min(max(audio_sizes), self.max_sample_size)
        else:
            audio_size = min(min(audio_sizes), self.max_sample_size)
        if audio is not None:
            collated_audios, padding_mask, audio_starts = self.collater_audio(audio, audio_size)
        else:
            collated_audios, audio_starts = None, None
        if video is not None:
            collated_videos, padding_mask, audio_starts = self.collater_audio(video, audio_size, audio_starts)
        else:
            collated_videos = None

        batch = {
            "id": torch.LongTensor([s["id"] for s in samples]),
            "file": [s["file"] for s in samples],
            "audio": collated_audios,
            "video": collated_videos,
            "padding_mask": padding_mask,
            "v_label": torch.LongTensor([s["v_label"] for s in samples]),
            "a_label": torch.LongTensor([s["a_label"] for s in samples]),
            "c_label": torch.LongTensor([s["c_label"] for s in samples]),
            "m_label": torch.LongTensor([s["m_label"] for s in samples]),
            "mm_label": torch.LongTensor([s["mm_label"] for s in samples]),
            "s_label": torch.LongTensor([s["s_label"] for s in samples]),
        }

        return batch

    def collater_audio(self, audios, audio_size, audio_starts=None):
        audio_feat_shape = list(audios[0].shape[1:])
        collated_audios = audios[0].new_zeros([len(audios), audio_size] + audio_feat_shape)
        padding_mask = torch.BoolTensor(len(audios), audio_size).fill_(False)  #
        start_known = audio_starts is not None
        audio_starts = [0 for _ in audios] if not start_known else audio_starts
        for i, audio in enumerate(audios):
            diff = len(audio) - audio_size
            if diff == 0:
                collated_audios[i] = audio
            elif diff < 0:
                assert self.pad_audio
                collated_audios[i] = torch.cat([audio, audio.new_full([-diff] + audio_feat_shape, 0.0)])
                padding_mask[i, diff:] = True
            else:
                collated_audios[i], audio_starts[i] = self.crop_to_max_size(
                    audio, audio_size, audio_starts[i] if start_known else None
                )
        if len(audios[0].shape) == 2:
            collated_audios = collated_audios.transpose(1, 2)  # [B, T, F] -> [B, F, T]
        else:
            collated_audios = collated_audios.permute(
                (0, 4, 1, 2, 3)
            ).contiguous()  # [B, T, H, W, C] -> [B, C, T, H, W]
        return collated_audios, padding_mask, audio_starts

    def train_dataloader(self) -> TRAIN_DATALOADERS:
        return DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
            collate_fn=self.collater,
            worker_init_fn=seed_worker,
            generator=g,
        )

    # def train_dataloader(self) -> TRAIN_DATALOADERS:
    #     weights = [40 if data['m_label'] == 1 else 1 for data in self.train_dataset]
    #     return DataLoader(self.train_dataset, batch_size=self.batch_size, num_workers=self.num_workers,
    #                       collate_fn=self.collater, sampler = WeightedRandomSampler(weights, num_samples=30000, replacement=True),
    #                       worker_init_fn=seed_worker, generator=g)
    #
    def val_dataloader(self) -> EVAL_DATALOADERS:
        if self.dataset_type == "original":
            return DataLoader(
                self.test_dataset,
                batch_size=self.batch_size,
                shuffle=False,
                num_workers=self.num_workers,
                collate_fn=self.collater,
                worker_init_fn=seed_worker,
                generator=g,
            )  # , worker_init_fn=seed_worker, generator=g
        else:
            return DataLoader(
                self.val_dataset,
                batch_size=self.batch_size,
                shuffle=False,
                num_workers=self.num_workers,
                collate_fn=self.collater,
                worker_init_fn=seed_worker,
                generator=g,
            )

    def test_dataloader(self) -> EVAL_DATALOADERS:
        return DataLoader(
            self.test_dataset,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
            collate_fn=self.collater,
            worker_init_fn=seed_worker,
            generator=g,
        )
