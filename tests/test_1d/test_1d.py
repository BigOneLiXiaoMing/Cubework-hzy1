import os
import sys
from functools import partial

import cubework
import torch.multiprocessing as mp

from check_1d_modules import (
    check_classifier_given_embed_weight,
    check_classifier_no_given_weight,
    check_embed,
    check_linear_col,
    check_linear_row,
    check_vocab_parallel_classifier_given_embed_weight,
    check_vocab_parallel_classifier_no_given_weight,
    check_vocab_parallel_embed,
    check_vocab_parallel_loss,
)


def run(rank, world_size, port):
    os.environ["RANK"] = str(rank)
    os.environ["LOCAL_RANK"] = str(rank)
    os.environ["WORLD_SIZE"] = str(world_size)
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = str(port)

    cubework.initialize_distributed()
    check_linear_col()
    check_linear_row()
    check_embed()
    check_classifier_no_given_weight()
    check_classifier_given_embed_weight()
    check_vocab_parallel_classifier_no_given_weight()
    check_vocab_parallel_classifier_given_embed_weight()
    check_vocab_parallel_embed()
    check_vocab_parallel_loss()


def test_distributed():
    world_size = 4
    tensor_parallel = "1d"
    tensor_parallel_size = 4
    sys.argv.append(f"--tp={tensor_parallel}")
    sys.argv.append(f"--tp_size={tensor_parallel_size}")

    run_func = partial(run, world_size=world_size, port=23333)
    mp.spawn(run_func, nprocs=world_size)


if __name__ == "__main__":
    test_distributed()
