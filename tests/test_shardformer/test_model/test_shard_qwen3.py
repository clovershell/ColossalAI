import pytest
import torch
import transformers

import colossalai
from colossalai.logging import disable_existing_loggers
from colossalai.shardformer.layer.utils import Randomizer
from colossalai.tensor.d_tensor.api import clear_layout_converter
from colossalai.testing import clear_cache_before_run, parameterize, rerun_if_address_is_in_use, spawn
from tests.kit.model_zoo import model_zoo
from tests.test_shardformer.test_model._utils import (
    build_model_from_hybrid_plugin,
    check_all_grad_tensors,
    check_loss,
    check_output_hidden_state,
    check_weight,
    get_grad_tensors_for_check,
    run_forward_backward_with_hybrid_plugin,
    unwrap_model,
)


def check_forward_backward(model_fn, data_gen_fn, output_transform_fn, loss_fn, test_config):
    org_model, org_optimizer, sharded_model, sharded_optimizer, criterion, booster = build_model_from_hybrid_plugin(
        model_fn, loss_fn, test_config
    )

    org_loss, org_output, sharded_loss, sharded_output = run_forward_backward_with_hybrid_plugin(
        org_model, sharded_model, sharded_optimizer, data_gen_fn, output_transform_fn, criterion, booster
    )

    stage_manager = booster.plugin.stage_manager
    tp_group = booster.plugin.tp_group

    # unwrap model
    qwen3_model = unwrap_model(org_model, "Qwen3Model", "model")
    shard_qwen3_model = unwrap_model(sharded_model, "Qwen3Model", "model")

    row_layer_for_check = ["layers[0].self_attn.q_proj", "embed_tokens"]
    col_layer_for_check = ["layers[0].self_attn.o_proj"]

    # Save gradient tensors for comparison between the original model and the sharded model before optimizer step.
    grads_to_check = {}
    if (stage_manager is None or stage_manager.is_first_stage(ignore_chunk=True)) and booster.plugin.zero_stage == 0:
        if test_config["precision"] == "fp32":
            atol, rtol = 1e-6, 1e-4
        else:
            atol, rtol = 5e-3, 5e-3
        row_layer_grads = get_grad_tensors_for_check(
            qwen3_model, shard_qwen3_model, row_layer_for_check, tp_group, atol=atol, rtol=rtol, dim=0, verbose=False
        )
        col_layer_grads = get_grad_tensors_for_check(
            qwen3_model, shard_qwen3_model, col_layer_for_check, tp_group, atol=atol, rtol=rtol, dim=1, verbose=False
        )
        grads_to_check.update(col_layer_grads)
        grads_to_check.update(row_layer_grads)

    # optimizer executes step
    org_optimizer.step()
    sharded_optimizer.step()

    # check last hidden state & loss
    if stage_manager is None or stage_manager.is_last_stage(ignore_chunk=True):
        if test_config["precision"] == "fp32":
            atol, rtol = 1e-5, 1e-3
        else:
            atol, rtol = 5e-3, 5e-3

        if org_model.__class__.__name__ == "Qwen3Model":
            check_output_hidden_state(org_output, sharded_output, stage_manager, atol=atol, rtol=rtol)

        check_loss(org_loss, sharded_loss, atol=atol, rtol=rtol)

    # check weights
    if stage_manager is None or stage_manager.is_first_stage(ignore_chunk=True):
        if test_config["precision"] == "fp32":
            atol, rtol = 1e-3, 1e-3
        else:
            atol, rtol = 5e-3, 5e-3
        check_weight(
            qwen3_model, shard_qwen3_model, col_layer_for_check, tp_group, atol=atol, rtol=rtol, dim=1, verbose=False
        )

    # check grads
    check_all_grad_tensors(grads_to_check)

    torch.cuda.empty_cache()


@parameterize(
    "test_config",
    [
        {
            "tp_size": 2,
            "pp_size": 2,
            "sp_size": 2,
            "num_microbatches": 2,
            "enable_sequence_parallelism": True,
            "sequence_parallelism_mode": "split_gather",
            "enable_flash_attention": True,
            "use_lazy_init": True,
            "zero_stage": 1,
            "precision": "fp16",
            "initial_scale": 1,
        },
        {  # Ulysess + Flash attention
            "tp_size": 1,
            "pp_size": 2,
            "sp_size": 2,
            "num_microbatches": 2,
            "enable_sequence_parallelism": True,
            "sequence_parallelism_mode": "all_to_all",
            "enable_flash_attention": True,
            "use_lazy_init": True,
            "zero_stage": 1,
            "precision": "fp16",
            "initial_scale": 1,
        },
        {
            "tp_size": 2,
            "pp_size": 2,
            "num_microbatches": 2,
            "enable_all_optimization": True,
            "use_lazy_init": True,
            "precision": "fp16",
            "initial_scale": 1,
        },
        {
            "tp_size": 1,
            "pp_size": 2,
            "num_microbatches": 4,
            "use_lazy_init": False,
            "precision": "fp32",
        },
        {
            "tp_size": 4,
            "pp_size": 1,
            "enable_all_optimization": True,
            "use_lazy_init": False,
            "precision": "fp32",
        },
        {
            "tp_size": 1,
            "pp_size": 4,
            "num_microbatches": 4,
            "enable_all_optimization": False,
            "use_lazy_init": False,
            "precision": "fp32",
        },
        {"tp_size": 2, "pp_size": 1, "enable_all_optimization": True, "use_lazy_init": False, "precision": "fp32"},
        {
            "tp_size": 2,
            "pp_size": 1,
            "enable_all_optimization": True,
            "use_lazy_init": True,
            "zero_stage": 2,
            "precision": "fp16",
            "initial_scale": 1,
        },
        {
            "tp_size": 2,
            "pp_size": 2,
            "sp_size": 2,
            "num_microbatches": 2,
            "enable_sequence_parallelism": True,
            "sequence_parallelism_mode": "ring",
            "enable_flash_attention": True,
            "use_lazy_init": True,
            "zero_stage": 1,
            "precision": "fp16",
            "initial_scale": 1,
        },
        {
            "tp_size": 1,
            "pp_size": 1,
            "sp_size": 2,
            "num_microbatches": 1,
            "enable_sequence_parallelism": True,
            "sequence_parallelism_mode": "all_to_all",
            "use_lazy_init": True,
            "zero_stage": 1,
            "precision": "fp16",
            "initial_scale": 1,
        },
        {
            "tp_size": 4,
            "pp_size": 1,
            "num_microbatches": 1,
            "enable_sequence_parallelism": True,
            "sequence_parallelism_mode": "split_gather",
            "enable_flash_attention": False,
            "use_lazy_init": True,
            "precision": "fp16",
            "initial_scale": 1,
        },
        {
            "tp_size": 1,
            "pp_size": 2,
            "num_microbatches": 2,
            "enable_all_optimization": True,
            "use_lazy_init": True,
            "zero_stage": 1,
            "precision": "fp16",
            "initial_scale": 1,
        },
    ],
)
def run_qwen3_test(test_config):
    sub_model_zoo = model_zoo.get_sub_registry("transformers_qwen3")

    for name, (model_fn, data_gen_fn, output_transform_fn, loss_fn, _) in sub_model_zoo.items():
        try:
            check_forward_backward(model_fn, data_gen_fn, output_transform_fn, loss_fn, test_config)
        except Exception as e:
            print(f"Failed config: {test_config}")
            raise e
    clear_layout_converter()
    Randomizer.reset_index()
    torch.cuda.empty_cache()


@parameterize(
    "test_config",
    [
        {
            "tp_size": 2,
            "pp_size": 2,
            "num_microbatches": 4,
            "enable_all_optimization": False,
            "use_lazy_init": False,
            "precision": "fp32",
            "initial_scale": 1,
        },
        {
            "tp_size": 2,
            "pp_size": 2,
            "num_microbatches": 4,
            "enable_all_optimization": False,
            "use_lazy_init": False,
            "precision": "fp16",
            "zero_stage": 1,
            "initial_scale": 1,
        },
        {
            "tp_size": 2,
            "pp_size": 2,
            "pp_style": "interleaved",
            "num_model_chunks": 2,
            "num_microbatches": 4,
            "enable_all_optimization": False,
            "precision": "fp16",
            "zero_stage": 1,
            "initial_scale": 1,
        },
    ],
)
def run_qwen3_3d_test(test_config):
    sub_model_zoo = model_zoo.get_sub_registry("transformers_qwen3")

    for name, (model_fn, data_gen_fn, output_transform_fn, loss_fn, _) in sub_model_zoo.items():
        try:
            check_forward_backward(model_fn, data_gen_fn, output_transform_fn, loss_fn, test_config)
        except Exception as e:
            print(f"Failed config: {test_config}")
            raise e

    clear_layout_converter()
    Randomizer.reset_index()
    torch.cuda.empty_cache()


def check_qwen3(rank, world_size, port):
    disable_existing_loggers()
    colossalai.launch(rank=rank, world_size=world_size, host="localhost", port=port, backend="nccl")
    run_qwen3_test()


def check_qwen3_3d(rank, world_size, port):
    disable_existing_loggers()
    colossalai.launch(rank=rank, world_size=world_size, host="localhost", port=port, backend="nccl")
    run_qwen3_3d_test()


@pytest.mark.skipif(transformers.__version__ < "4.51.0", reason="Requires transformers version 4.51.0 or later")
@pytest.mark.dist
@rerun_if_address_is_in_use()
@clear_cache_before_run()
def test_qwen3():
    spawn(check_qwen3, 4)


@pytest.mark.skipif(transformers.__version__ < "4.51.0", reason="Requires transformers version 4.51.0 or later")
@pytest.mark.largedist
@rerun_if_address_is_in_use()
@clear_cache_before_run()
def test_qwen3_3d():
    spawn(check_qwen3_3d, 8)


if __name__ == "__main__":
    test_qwen3()
    test_qwen3_3d()
