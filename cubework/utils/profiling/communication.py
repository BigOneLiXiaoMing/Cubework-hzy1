import time
from functools import partial
from typing import List

import torch.distributed as dist
from torch import Tensor
from torch.distributed import ReduceOp


class CommProfiler(object):
    def __init__(self) -> None:
        self.reset()

    def start(self):
        dist.all_reduce = partial(all_reduce, profiler=self)
        dist.all_gather = partial(all_gather, profiler=self)
        dist.reduce_scatter = partial(reduce_scatter, profiler=self)
        dist.broadcast = partial(broadcast, profiler=self)
        dist.reduce = partial(reduce, profiler=self)

    def stop(self):
        dist.all_reduce = torch_all_reduce
        dist.all_gather = torch_all_gather
        dist.reduce_scatter = torch_reduce_scatter
        dist.broadcast = torch_broadcast
        dist.reduce = torch_reduce

    def new(self, vol):
        self.running_ops += 1
        self.total_cnt += 1
        self.total_vol += vol
        if self.start_time is None:
            self.start_time = time.time()

    def finish(self):
        self.running_ops -= 1
        if self.running_ops == 0:
            self.total_time += time.time() - self.start_time
            self.start_time = None

    def reset(self):
        self.running_ops = 0
        self.total_time = 0
        self.total_vol = 0
        self.total_cnt = 0
        self.start_time = None


class CommHandler(object):
    def __init__(self, profiler, work):
        super().__init__()
        self.prof = profiler
        self.work = work

    def wait(self):
        self.work.wait()
        self.prof.finish()


torch_all_reduce = dist.all_reduce
torch_all_gather = dist.all_gather
torch_reduce_scatter = dist.reduce_scatter
torch_broadcast = dist.broadcast
torch_reduce = dist.reduce


def all_reduce(
    tensor: Tensor, op: ReduceOp = ReduceOp.SUM, group=None, async_op: bool = False, profiler: CommProfiler = None
):
    comm_size = dist.get_world_size(group)
    correction = 2 * (comm_size - 1) / comm_size
    comm_vol = correction * tensor.element_size() * tensor.numel()
    profiler.new(comm_vol)
    work = torch_all_reduce(tensor, op, group, async_op)

    if async_op:
        return CommHandler(profiler, work)
    else:
        profiler.finish()


def reduce_scatter(
    output: Tensor,
    input_list: List[Tensor],
    op: ReduceOp = ReduceOp.SUM,
    group=None,
    async_op: bool = False,
    profiler: CommProfiler = None,
):
    comm_size = dist.get_world_size(group)
    correction = (comm_size - 1) / comm_size
    comm_vol = 0
    for tensor in input_list:
        comm_vol += tensor.element_size() * tensor.numel()
    comm_vol *= correction
    profiler.new(comm_vol)
    work = torch_reduce_scatter(output, input_list, op, group, async_op)

    if async_op:
        return CommHandler(profiler, work)
    else:
        profiler.finish()


def all_gather(
    tensor_list: List[Tensor],
    tensor: Tensor,
    group=None,
    async_op: bool = False,
    profiler: CommProfiler = None,
):
    comm_size = dist.get_world_size(group)
    correction = (comm_size - 1) / comm_size
    comm_vol = 0
    for ten in tensor_list:
        comm_vol += ten.element_size() * ten.numel()
    comm_vol *= correction
    profiler.new(comm_vol)
    work = torch_all_gather(tensor_list, tensor, group, async_op)

    if async_op:
        return CommHandler(profiler, work)
    else:
        profiler.finish()


def broadcast(tensor: Tensor, src: int, group=None, async_op: bool = False, profiler: CommProfiler = None):
    comm_vol = 1.0 * tensor.element_size() * tensor.numel()
    profiler.new(comm_vol)
    work = torch_broadcast(tensor, src, group, async_op)

    if async_op:
        return CommHandler(profiler, work)
    else:
        profiler.finish()


def reduce(
    tensor: Tensor,
    dst: int,
    op: ReduceOp = ReduceOp.SUM,
    group=None,
    async_op: bool = False,
    profiler: CommProfiler = None,
):
    comm_vol = 1.0 * tensor.element_size() * tensor.numel()
    profiler.new(comm_vol)
    work = torch_reduce(tensor, dst, op, group, async_op)

    if async_op:
        return CommHandler(profiler, work)
    else:
        profiler.finish()
