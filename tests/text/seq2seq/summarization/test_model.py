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
import os
import re
from unittest import mock

import pytest
import torch

from flash import DataKeys, Trainer
from flash.core.utilities.imports import _SERVE_TESTING, _TEXT_AVAILABLE, _TEXT_TESTING
from flash.text import SummarizationTask

# ======== Mock functions ========


class DummyDataset(torch.utils.data.Dataset):
    def __getitem__(self, index):
        return {
            "input_ids": torch.randint(1000, size=(128,)),
            DataKeys.TARGET: torch.randint(1000, size=(128,)),
        }

    def __len__(self) -> int:
        return 100


# ==============================

TEST_BACKBONE = "sshleifer/tiny-mbart"  # tiny model for testing


@pytest.mark.skipif(os.name == "nt", reason="Huggingface timing out on Windows")
@pytest.mark.skipif(not _TEXT_TESTING, reason="text libraries aren't installed.")
def test_init_train(tmpdir):
    model = SummarizationTask(TEST_BACKBONE)
    train_dl = torch.utils.data.DataLoader(DummyDataset())
    trainer = Trainer(default_root_dir=tmpdir, fast_dev_run=True)
    trainer.fit(model, train_dl)


@pytest.mark.skipif(not _TEXT_TESTING, reason="text libraries aren't installed.")
def test_jit(tmpdir):
    sample_input = {
        "input_ids": torch.randint(128, size=(1, 32)),
        "attention_mask": torch.randint(1, size=(1, 32)),
    }
    path = os.path.join(tmpdir, "test.pt")

    model = SummarizationTask(TEST_BACKBONE)
    model.eval()

    # Huggingface only supports `torch.jit.trace`
    model = torch.jit.trace(model, [sample_input], check_trace=False)

    torch.jit.save(model, path)
    model = torch.jit.load(path)

    out = model(sample_input)
    assert isinstance(out, torch.Tensor)


@pytest.mark.skipif(not _SERVE_TESTING, reason="serve libraries aren't installed.")
@mock.patch("flash._IS_TESTING", True)
def test_serve():
    model = SummarizationTask(TEST_BACKBONE)
    model.eval()
    model.serve()


@pytest.mark.skipif(_TEXT_AVAILABLE, reason="text libraries are installed.")
def test_load_from_checkpoint_dependency_error():
    with pytest.raises(ModuleNotFoundError, match=re.escape("'lightning-flash[text]'")):
        SummarizationTask.load_from_checkpoint("not_a_real_checkpoint.pt")
