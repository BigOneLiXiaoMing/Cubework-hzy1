import math
import time

import cubework
from cubework.arguments import parse_args
from cubework.distributed.collective import all_reduce
import torch
from cubework.utils import (
    MemoryTracker,
    CommProfiler,
    get_current_device,
    get_logger,
    write_logger_to_file,
    calc_model_size,
    calc_tflops,
    clip_grad_norm,
)
from cubework.distributed import ParallelManager as pm
from tqdm import tqdm
import argparse
from .gpt2 import build_gpt2


_builder = {
    "gpt2": build_gpt2,
}


logger = None
mem_tracker = None
comm_profiler = None

model = None
train_data = None
test_data = None
criterion = None
metric = None
optimizer = None
scaler = None
lr_scheduler = None

numel = None


def _data_parallel_sum(tensor):
    out = tensor
    if pm.DATA.world_size > 1:
        out = all_reduce(out, pm.DATA)
    return out


def _data_parallel_mean(tensor):
    out = tensor
    if pm.DATA.world_size > 1:
        out = all_reduce(out, pm.DATA) / pm.DATA.world_size
    return out


def _train(epoch, args):
    model.train()

    num_steps = len(train_data)
    if args.steps_per_epoch is not None and args.steps_per_epoch < num_steps:
        num_steps = args.steps_per_epoch
    progress = range(num_steps)

    if pm.GLOBAL.rank == 0:
        progress = tqdm(progress, desc=f"[Epoch {epoch} / Train]")

    total_loss = torch.zeros(()).to(torch.float).to(get_current_device())
    total_time = 0.0
    total_steps = 0
    total_samples = torch.zeros(()).to(torch.int).to(get_current_device())
    total_tokens = torch.zeros(()).to(torch.int).to(get_current_device())

    data_iter = iter(train_data)

    if comm_profiler is not None:
        comm_profiler.reset()
        comm_profiler.start()

    if mem_tracker is not None:
        mem_tracker.start()

    for i in progress:
        fwd_start = time.time()

        batch = next(data_iter)

        labels = batch.pop("labels")
        batch_size = None
        batch_tokens = None
        if isinstance(labels, torch.Tensor):
            labels = labels.to(get_current_device())
            batch_size = labels.size(0)
            batch_tokens = labels.numel()
        else:
            for k, v in labels.items():
                labels[k] = v.to(get_current_device())
                if batch_size is None:
                    batch_size = v.size(0)
                if batch_tokens is None:
                    batch_tokens = v.numel()

        for k, v in batch.items():
            batch[k] = v.to(get_current_device())

        if args.use_mixed_precision:
            with torch.cuda.amp.autocast():
                outputs = model(**batch)
        else:
            outputs = model(**batch)

        loss = criterion(outputs, labels)
        total_loss += loss

        fwd_end = time.time()

        bwd_start = time.time()

        if scaler is not None:
            scaler.scale(loss).backward()
        else:
            loss.backward()

        if (i + 1) % args.gradient_accumulation or i + 1 == num_steps:
            if scaler is not None:
                scaler.unscale_(optimizer)
                if args.gradient_clipping > 0:
                    clip_grad_norm(model.parameters(), args.gradient_clipping)
                scaler.step(optimizer)
                scaler.update()

            else:
                if args.gradient_clipping > 0:
                    clip_grad_norm(model.parameters(), args.gradient_clipping)
                optimizer.step()

        lr_scheduler.step()

        bwd_end = time.time()

        total_steps += 1
        total_samples += batch_size
        total_tokens += batch_tokens

        fwd_time = fwd_end - fwd_start
        bwd_time = bwd_end - bwd_start
        batch_time = fwd_time + bwd_time
        total_time += batch_time

        if pm.GLOBAL.rank == 0:
            progress.set_postfix(
                loss=loss.item(),
                lr=lr_scheduler.get_last_lr()[0],
                time_forward=fwd_time,
                time_backward=bwd_time,
                throughput=batch_size * pm.GLOBAL.world_size / (batch_time + 1e-12),
                tflops=calc_tflops(batch_time, batch_tokens * pm.GLOBAL.world_size),
            )

    if mem_tracker is not None:
        peak_mem = mem_tracker.stop()

    if comm_profiler is not None:
        _, comm_vol, comm_time = comm_profiler.stop()

    total_loss = _data_parallel_mean(total_loss)
    total_samples = _data_parallel_sum(total_samples)
    total_tokens = _data_parallel_sum(total_tokens)

    msg = f"[Epoch {epoch} / Train]: Loss = {total_loss.item() / num_steps:.3f}"
    msg += f" | Throughput = {total_samples.item() / (total_time + 1e-12):.3f} samples/sec"
    tflops = calc_tflops(
        numel, total_tokens.item(), total_time, with_backward=True, checkpoint=args.use_activation_checkpoint
    )
    msg += f" | TFLOPS = {tflops:.3f}"
    if mem_tracker is not None:
        msg += f" | Peak memory = {peak_mem / 1024:.3f} GB"
    if comm_profiler is not None:
        msg += (
            f"\n[Epoch {epoch} / Train]: Communication time = {comm_time:.3f} s, "
            + f"ratio = {comm_time * 100 / (total_time + 1e-12):.3f} %, "
            + f"avg bandwidth = {(comm_vol / 1024**2) / (comm_time + 1e-12):.3f} MB/s"
        )
    logger.info(msg)


def _test(epoch, args):
    model.eval()

    num_steps = len(test_data)
    if args.steps_per_epoch is not None and args.steps_per_epoch < num_steps:
        num_steps = args.steps_per_epoch
    progress = range(num_steps)

    if pm.GLOBAL.rank == 0:
        progress = tqdm(progress, desc=f"[Epoch {epoch} / Test]")

    total_loss = torch.zeros(()).to(torch.float).to(get_current_device())
    total_time = 0.0
    total_steps = 0
    total_samples = torch.zeros(()).to(torch.int).to(get_current_device())
    total_tokens = torch.zeros(()).to(torch.int).to(get_current_device())
    total_metric = 0.0

    data_iter = iter(test_data)

    if comm_profiler is not None:
        comm_profiler.reset()
        comm_profiler.start()

    if mem_tracker is not None:
        mem_tracker.start()

    with torch.no_grad():
        for _ in progress:
            batch_start = time.time()

            batch = next(data_iter)

            labels = batch.pop("labels")
            batch_size = None
            batch_tokens = None
            if isinstance(labels, torch.Tensor):
                labels = labels.to(get_current_device())
                batch_size = labels.size(0)
                batch_tokens = labels.numel()
            else:
                for k, v in labels.items():
                    labels[k] = v.to(get_current_device())
                    if batch_size is None:
                        batch_size = v.size(0)
                    if batch_tokens is None:
                        batch_tokens = v.numel()

            for k, v in batch.items():
                batch[k] = v.to(get_current_device())
            if args.use_mixed_precision:
                with torch.cuda.amp.autocast():
                    outputs = model(**batch)
            else:
                outputs = model(**batch)

            loss = criterion(outputs, labels)
            total_loss += loss

            batch_end = time.time()

            total_steps += 1
            total_samples += batch_size
            total_tokens += batch_tokens

            batch_time = batch_end - batch_start
            total_time += batch_time

            if pm.GLOBAL.rank == 0:
                metrics = dict(
                    loss=loss.item(),
                    step_time=batch_time,
                    throughput=batch_size * world_size / (batch_time + 1e-12),
                    tflops=get_tflops(batch_time, batch_tokens * world_size),
                )
                if evaluation == "ppl":
                    metrics["perplexity"] = math.exp(loss.item())
                elif evaluation == "acc":
                    if not isinstance(labels, torch.Tensor):
                        labels = labels["targets_a"]
                    batch_correct = torch.sum(labels == torch.argmax(outputs, dim=-1)).item()
                    metrics["accuracy"] = batch_correct / batch_size
                    correct += batch_correct
                else:
                    raise ValueError(f"Invalid evaluation method {evaluation}")
                progress.set_postfix(**metrics)

    peak_mem = None
    if mem_monitor is not None:
        peak_mem = max(mem_monitor.finish())

    all_reduce(test_loss)
    reduced_loss = test_loss.item() / (world_size * num_steps)
    all_reduce(num_samples)
    all_reduce(num_tokens)
    if evaluation == "acc":
        all_reduce(correct)

    msg = f"[Epoch {epoch} / Test]: Loss = {reduced_loss:.3f}"
    if evaluation == "ppl":
        msg += f" | Perplexity = {math.exp(reduced_loss):.3f}"
    else:
        msg += f" | Accuracy = {correct.item() * 100 / num_samples.item():.3f} %"
    msg += f" | Throughput = {num_samples.item() / (used_time + 1e-12):.3f} samples/sec"
    msg += f" | TFLOPS = {get_tflops(used_time, num_tokens.item()):.3f}"
    if peak_mem is not None:
        msg += f" | Peak memory = {peak_mem / 1024:.3f} GB."
    print_log(msg)


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--use_mem_tracker", action="store_true")
    parser.add_argument("--use_comm_profiler", action="store_true")
    parser.add_argument("--model_name", "--m", type=str)
    parser.add_argument("--batch_size", "--bs", type=int)
    parser.add_argument("--num_epochs", "--n_epoch", type=int)
    parser.add_argument("--steps_per_epoch", "--n_step", type=int)
    parser.add_argument("--learning_rate", "--lr", type=float)
    parser.add_argument("--weight_decay", "--decay", type=float)
    parser.add_argument("--use_activation_checkpoint", "--ac", action="store_true", default=False)
    parser.add_argument("--gradient_clipping", "--gc", type=float, default=0.0)
    parser.add_argument("--gradient_accumulation", "--ga", type=int, default=1)
    parser.add_argument("--use_mixed_precision", "--amp", action="store_true")
    parser.add_argument("--fp16_initial_scale", type=float, default=2**15)
    parser.add_argument("--fp16_growth_factor", type=float, default=2.0)
    parser.add_argument("--fp16_backoff_factor", type=float, default=0.5)
    parser.add_argument("--fp16_growth_interval", type=int, default=1000)
    parser.add_argument("--log_file", type=str)
    return parser


def train():
    parser = get_parser()
    cubework.initialize_distributed(parser)

    args = parse_args(parser)

    logger = get_logger()
    if args.log_file is not None:
        write_logger_to_file(logger)

    model_type = args.model_name.split("_")[0]
    assert model_type in ["gpt2", "vit"], f"No support for {model}."

    global model, train_data, test_data, criterion, optimizer, lr_scheduler
    model, train_data, test_data, criterion, metric, optimizer, lr_scheduler = _builder[model_type](args)

    global scaler
    if args.use_mixed_precision:
        scaler = torch.cuda.amp.GradScaler(
            enabled=True,
            initial_scale=args.fp16_initial_scale,
            growth_factor=args.fp16_growth_factor,
            backoff_factor=args.fp16_backoff_factor,
            growth_interval=args.fp16_growth_interval,
        )

    global mem_tracker
    if args.use_mem_monitor:
        mem_tracker = MemoryTracker(args.log_file)

    global comm_profiler
    if args.use_comm_profiler:
        comm_profiler = CommProfiler()

    global numel
    numel = calc_model_size(model)
    if numel < 1e9:
        msg = f"{numel / 1e6:.3f} M"
    else:
        msg = f"{numel / 1e9:.3f} B"
    logger.info(f"Model is built (parameter size = {msg}).")

    logger.info("Benchmark start.")

    for epoch in range(args.num_epochs):
        _train(epoch, args)
        _test(epoch, args)

    logger.info("Benchmark complete.")


if __name__ == "__main__":
    train()
