from __future__ import annotations
from typing import TYPE_CHECKING

import torch
from torch.fx.passes.split_module import split_module

from thunder.dynamo.utils import (
    SubgraphInfo,
    CompiledFunction,
    CompilerType,
    SplitReason,
    SplitReasonType,
    is_node_supported_by_thunder,
    get_nodes_in_unsupported_ctx_regions,
    update_node_and_submodule,
    recompile_graph,
    checkpoint_converter,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _splitter(
    gm: torch.fx.GraphModule,
    thunder_jit: Callable,
    torch_inductor: Callable,
    _unused_sample_args: list[torch.SymInt, torch.Tensor],
) -> tuple[torch.fx.GraphModule, SubgraphInfo]:
    """
    This method will split graph into multiple graph modules based on thunder supported operations.
    This function will try to split the graph in contiguous partitions.

    Example:
        # All operations are supported by thunder
        class GraphModule(torch.nn.Module):
            def forward(self, L_x_: "f32[2]"):
                l_x_ = L_x_

                y: "f32[2]" = torch.sin(l_x_)
                matmul: "f32[]" = torch.matmul(l_x_, y);  l_x_ = y = None
                return (matmul,)

        # Split Graph: All operations are supported by thunder, we will see only one partition.
        class GraphModule(torch.nn.Module):
            def forward(self, l_x_: "f32[2]"):
                thunder_1 = self.thunder_1(l_x_);  l_x_ = None
                return (thunder_1,)

            class thunder_1(torch.nn.Module):
                def forward(self, l_x_: "f32[2]"):
                    y: "f32[2]" = torch.sin(l_x_)
                    matmul: "f32[]" = torch.matmul(l_x_, y);  l_x_ = y = None
                    return matmul

    Example:
        # With unsupported operation `sinc`
        class GraphModule(torch.nn.Module):
            def forward(self, L_x_: "f32[2]"):
                l_x_ = L_x_

                y: "f32[2]" = torch.sinc(l_x_)

                matmul: "f32[]" = torch.matmul(l_x_, y);  l_x_ = y = None
                return (matmul,)

        # Split Graph: Since `sinc` is unsupported, we will see two partitions, one for thunder and one for inductor.
        class GraphModule(torch.nn.Module):
            def forward(self, l_x_: "f32[2]"):
                inductor_1 = self.inductor_1(l_x_)
                thunder_2 = self.thunder_2(l_x_, inductor_1);  l_x_ = inductor_1 = None
                return (thunder_2,)

            class inductor_1(torch.nn.Module):  # Partition for inductor
                def forward(self, l_x_: "f32[2]"):
                    y: "f32[2]" = torch.sinc(l_x_);  l_x_ = None
                    return y

            class thunder_2(torch.nn.Module):  # Partition for thunder
                def forward(self, l_x_: "f32[2]", y: "f32[2]"):
                    matmul: "f32[]" = torch.matmul(l_x_, y);  l_x_ = y = None
                    return matmul
    """
    # The callback below is called for every node in the graph.
    # It returns an `int` denoting the parition where the node should be placed.
    # We want to partition the graph into contiguous regions (with one or more operations)
    # into thunder supported or unsupported region.
    # `prev_value` is used to determine if we are still in same region (i.e. supported region or unsupported region).
    # `partition_cnt` is bumped everytime we change the region i.e. flip from supported to unsupported or from unsupported to supported.
    # `supported_partitions` is used to track the thunder supported partitions.
    prev_value = None
    partition_cnt = 0
    supported_partitions: set[int] = set()
    split_reasons: list[SplitReason] = []
    has_thunder_tracable_checkpoint = False

    nodes_in_unsupported_ctx_regions = get_nodes_in_unsupported_ctx_regions(gm)

    def callback(node) -> int:
        nonlocal prev_value, partition_cnt, split_reasons, supported_partitions, has_thunder_tracable_checkpoint

        assert node.op not in (
            "placeholder",
            "get_attr",
            "output",
        ), f"fx.split_module should have only passed node.op=call_* but received {node.op}"

        if node in nodes_in_unsupported_ctx_regions:
            # If node was in unsupported ctx region like `autocast`,
            # even though the operation maybe supported, we pass it to `torch.compile`
            # as `thunder` doesn't correctly work with these.
            is_thunder_supported = False
            split_reason = SplitReason(
                SplitReasonType.UNSUPPORTED_NODE,
                info=f"node with name: {node.name} and target: {node.target} is not supported probably because it is in unsupported context.",
            )
            split_reasons.append(split_reason)
        else:
            is_thunder_supported, split_reason = is_node_supported_by_thunder(node)
            if node.target is torch.ops.higher_order.tag_activation_checkpoint and is_thunder_supported:
                has_thunder_tracable_checkpoint = True
            if split_reason is not None:
                split_reasons.append(split_reason)

        if prev_value == is_thunder_supported:  # We are in the same region.
            return partition_cnt

        # There is a flip. Either from supported to unsupported or unsupported to supported.
        prev_value = is_thunder_supported
        partition_cnt += 1  # Bump the region cnt.

        if is_thunder_supported:
            supported_partitions.add(partition_cnt)
        return partition_cnt

    # `split_module` iterates over nodes and determines the partition to place them based on the callback.
    split_gm: torch.fx.GraphModule = split_module(
        gm, root_m=None, split_callback=callback, keep_original_order=True, keep_original_node_name=True
    )

    def is_thunder_supported_partition(node: torch.fx.Node) -> bool:
        return node.name.startswith("submod") and int(node.name.replace("submod_", "")) in supported_partitions

    # Call compile on the split region/s.
    thunder_compiled_fns = []
    submodule_to_compiled_fns = {}

    # If the split_gm contains Thunder-traceable checkpoint operators, the checkpointed submodules are replaced, so we need to record the original split_gm
    origin_split_gm = split_gm
    if has_thunder_tracable_checkpoint:
        import copy

        split_gm = copy.deepcopy(split_gm)

    for node in split_gm.graph.nodes:
        if is_thunder_supported_partition(node):
            graph_module = getattr(split_gm, node.name)
            # Replace the torch operators within the function called by activation checkpoint with the corresponding Thunder symbols
            checkpoint_converter(split_gm, graph_module)
            jit_fn = thunder_jit(graph_module)
            # Update the node name from "submod_*" to "thunder_*" for more user-friendly names
            update_node_and_submodule(split_gm, node, node.name.replace("submod", "thunder"), jit_fn)
            thunder_compiled_fns.append(jit_fn)
            if has_thunder_tracable_checkpoint:
                submodule_to_compiled_fns[getattr(origin_split_gm, node.name.replace("thunder", "submod"))] = (
                    CompiledFunction(jit_fn, CompilerType.THUNDER)
                )
            else:
                submodule_to_compiled_fns[graph_module] = CompiledFunction(jit_fn, CompilerType.THUNDER)
        elif node.name.startswith("submod"):  # For inductor
            graph_module = getattr(split_gm, node.name)
            jit_fn = torch_inductor(graph_module)
            # Update the node name from "submod_*" to "inductor_*" for more user-friendly names
            update_node_and_submodule(split_gm, node, node.name.replace("submod", "inductor"), jit_fn)
            if has_thunder_tracable_checkpoint:
                submodule_to_compiled_fns[getattr(origin_split_gm, node.name.replace("inductor", "submod"))] = (
                    CompiledFunction(jit_fn, CompilerType.TORCH_INDUCTOR)
                )
            else:
                submodule_to_compiled_fns[graph_module] = CompiledFunction(jit_fn, CompilerType.TORCH_INDUCTOR)
        else:
            # Everything else is a glue code to call and pass outputs between the other partitions.
            pass

    # We update the GraphModule in `update_node_and_submodule`, so we need to recompile.
    recompile_graph(split_gm)

    return split_gm, SubgraphInfo(
        gm,
        origin_split_gm,
        split_gm,
        thunder_compiled_fns,
        submodule_to_compiled_fns,
        split_reasons,
    )
