import os

import torch.distributed as dist

import cubework.distributed as cube_dist
from cubework.arguments import parse_args
from cubework.global_vars import ALLOWED_MODES, env
from cubework.utils import set_device, set_seed

_DEFAULT_SEED = 1024


def initialize_distributed(parser=None):
    args = parse_args(parser)

    rank = int(os.environ["RANK"])
    local_rank = int(os.environ["LOCAL_RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    addr = os.environ["MASTER_ADDR"]
    port = int(os.environ["MASTER_PORT"])

    init_method = f"tcp://{addr}:{port}"
    backend = "nccl" if args.backend is None else args.backend
    dist.init_process_group(rank=rank, world_size=world_size, backend=backend, init_method=init_method)
    cube_dist.init_global()

    data_parallel_size = world_size if args.tensor_parallel_size is None else world_size // args.tensor_parallel_size
    cube_dist.init_data_parallel(data_parallel_size)

    seed = args.seed if args.seed is not None else _DEFAULT_SEED
    set_seed(seed)

    env.mode = args.tensor_parallel
    assert env.mode in ALLOWED_MODES
    if args.tensor_parallel is not None:
        cube_dist.init_tensor_parallel(args.tensor_parallel_size, seed)

    set_device(local_rank)
