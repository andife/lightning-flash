# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import Any, Callable, List, Mapping, Optional, Sequence, Type, Union, Dict

import torch
import pytorch_lightning as pl

from torch import nn
from torch.optim import Optimizer

from flash.core.model import Task
from flash.core.data.data_source import DefaultDataKeys
from flash.core.data.process import Postprocess
from flash.core.data.process import Preprocess, Serializer
from flash.core.utilities.imports import _FASTFACE_AVAILABLE
from flash.core.finetuning import FlashBaseFinetuning
from flash.image.face_detection.data import FaceDetectionPreprocess, FaceDetectionPostProcess

if _FASTFACE_AVAILABLE:
    import fastface as ff


class FaceDetectionFineTuning(FlashBaseFinetuning):
    def __init__(self, train_bn: bool = True) -> None:
        super().__init__(train_bn=train_bn)

    def freeze_before_training(self, pl_module: pl.LightningModule) -> None:
        self.freeze(modules=pl_module.model.backbone, train_bn=self.train_bn)


class DetectionLabels(Serializer):
    """A :class:`.Serializer` which extracts predictions from sample dict."""

    def serialize(self, sample: Any) -> Dict[str, Any]:
        sample = sample[DefaultDataKeys.PREDS] if isinstance(sample, Dict) else sample
        return sample


class FaceDetector(Task):
    """The ``FaceDetector`` is a :class:`~flash.Task` for detecting faces in images. For more details, see
    :ref:`face_detection`.
    Args:
        model: a string of :attr`_models`. Defaults to 'lffd_slim'.
        pretrained: Whether the model from fastface should be loaded with it's pretrained weights.
        loss: the function(s) to update the model with. Has no effect for fastface models.
        metrics: The provided metrics. All metrics here will be logged to progress bar and the respective logger.
            Changing this argument currently has no effect.
        optimizer: The optimizer to use for training. Can either be the actual class or the class name.
        learning_rate: The learning rate to use for training
    """

    required_extras: str = "image"

    def __init__(
        self,
        model: str = "lffd_slim",
        pretrained: bool = True,
        loss=None,
        metrics: Union[Callable, nn.Module, Mapping, Sequence, None] = None,
        optimizer: Type[Optimizer] = torch.optim.AdamW,
        learning_rate: float = 1e-4,
        serializer: Optional[Union[Serializer, Mapping[str, Serializer]]] = None,
        preprocess: Optional[Preprocess] = None,
        **kwargs: Any,
    ):
        self.save_hyperparameters()

        if model in ff.list_pretrained_models():
            model = FaceDetector.get_model(model, pretrained, **kwargs)
        else:
            ValueError(f"{model} is not supported yet.")

        super().__init__(
            model=model,
            loss_fn=loss,
            metrics=metrics or {"AP": ff.metric.AveragePrecision()},
            learning_rate=learning_rate,
            optimizer=optimizer,
            serializer=serializer or DetectionLabels(),
            preprocess=preprocess or FaceDetectionPreprocess(),
        )

    @staticmethod
    def get_model(
        model_name,
        pretrained,
        **kwargs,
    ):

        if pretrained:
            pl_model = ff.FaceDetector.from_pretrained(model_name, **kwargs)
        else:
            arch, config = model_name.split("_")
            pl_model = ff.FaceDetector.build(arch, config, **kwargs)

        # get torch.nn.Module
        model = getattr(pl_model, "arch")

        # set preprocess params
        model.register_buffer("normalizer", getattr(pl_model, "normalizer"))
        model.register_buffer("mean", getattr(pl_model, "mean"))
        model.register_buffer("std", getattr(pl_model, "std"))

        # set postprocess function
        setattr(model, "_postprocess", getattr(pl_model, "_postprocess"))

        return model

    def forward(self, x: List[torch.Tensor]) -> Any:
        images = self._prepare_batch(x)
        logits = self.model(images)

        # preds: torch.Tensor(B, N, 5)
        # preds: torch.Tensor(N, 6) as x1,y1,x2,y2,score,batch_idx
        preds = self.model.logits_to_preds(logits)
        preds = self.model._postprocess(preds)

        return preds

    def _prepare_batch(self, batch):
        batch = (((batch * 255) / self.model.normalizer) - self.model.mean) / self.model.std
        return batch

    def _compute_metrics(self, logits, targets):
        preds = self.model.logits_to_preds(logits)
        # preds: torch.Tensor(B, N, 5)

        preds = self.model._postprocess(preds)
        # preds: torch.Tensor(N, 6) as x1,y1,x2,y2,score,batch_idx

        target_boxes = [target["target_boxes"] for target in targets]
        pred_boxes = [preds[preds[:, 5] == batch_idx, :5] for batch_idx in range(len(targets))]

        for metric in self.val_metrics.values():
            metric.update(pred_boxes, target_boxes)

    def shared_step(self, batch, train=False) -> Any:
        images, targets = batch[DefaultDataKeys.INPUT], batch[DefaultDataKeys.TARGET]
        images = self._prepare_batch(images)
        logits = self.model(images)
        loss = self.model.compute_loss(logits, targets)

        if not train:
            self._compute_metrics(logits, targets)

        return loss, logits

    def training_step(self, batch, batch_idx) -> Any:
        loss, _ = self.shared_step(batch)

        self.log_dict({f"train_{k}": v for k, v in loss.items()}, on_step=True, on_epoch=True, prog_bar=True)
        return loss

    def on_validation_epoch_start(self) -> None:
        for metric in self.val_metrics.values():
            metric.reset()

    def validation_step(self, batch, batch_idx):
        loss, logits = self.shared_step(batch)

        self.log_dict({f"val_{k}": v for k, v in loss.items()}, on_step=True, on_epoch=True, prog_bar=True)
        return loss

    def validation_epoch_end(self, outputs) -> None:
        metric_results = {name: metric.compute() for name, metric in self.val_metrics.items()}
        self.log_dict({f"val_{k}": v for k, v in metric_results.items()}, on_epoch=True)

    def on_test_epoch_start(self) -> None:
        for metric in self.val_metrics.values():
            metric.reset()

    def test_step(self, batch, batch_idx):
        loss, logits = self.shared_step(batch)

        self.log_dict({f"test_{k}": v for k, v in loss.items()}, on_step=True, on_epoch=True, prog_bar=True)
        return loss

    def test_epoch_end(self, outputs) -> None:
        metric_results = {name: metric.compute() for name, metric in self.val_metrics.items()}
        self.log_dict({f"test_{k}": v for k, v in metric_results.items()}, on_epoch=True)

    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: int = 0) -> Any:
        images = batch[DefaultDataKeys.INPUT]
        batch[DefaultDataKeys.PREDS] = self(images)
        return batch

    def configure_finetune_callback(self):
        return [FaceDetectionFineTuning()]
