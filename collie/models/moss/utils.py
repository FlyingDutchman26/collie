import gc

import torch

from collie.utils import is_pipeline, pipline_layers_idx, pipline_parts

def create_sinusoidal_positions(num_pos: int, dim: int) -> torch.Tensor:
    inv_freq = 1.0 / (10000 ** (torch.arange(0, dim, 2) / dim))
    sinusoid_inp = torch.einsum("i , j -> i j", torch.arange(num_pos, dtype=torch.float), inv_freq).float()
    return torch.cat((torch.sin(sinusoid_inp), torch.cos(sinusoid_inp)), dim=1)


def rotate_every_two(x: torch.Tensor) -> torch.Tensor:
    x1 = x[:, :, :, ::2]
    x2 = x[:, :, :, 1::2]
    x = torch.stack((-x2, x1), dim=-1)
    return x.flatten(-2)  # in einsum notation: rearrange(x, '... d j -> ... (d j)')


def apply_rotary_pos_emb(tensor: torch.Tensor, sin: torch.Tensor, cos: torch.Tensor) -> torch.Tensor:
    sin = torch.repeat_interleave(sin[:, :, None, :], 2, 3)
    cos = torch.repeat_interleave(cos[:, :, None, :], 2, 3)
    return (tensor * cos) + (rotate_every_two(tensor) * sin)

def _name_to_pipline(name):
    max_pipe_idx = max(pipline_parts())
    if name.startswith("transformer.wte."):
        pipe_name = name.replace("transformer.wte.", "0.")
    elif name.startswith("lm_head."):
        pipe_name = name.replace("lm_head.", f"{max_pipe_idx - 1}.")
    elif name.startswith("transformer.ln_f."):
        pipe_name = name.replace("transformer.ln_f.", f"{max_pipe_idx - 2}.")
    else:
        assert name.startswith("transformer.h."), name
        assert name.split(".")[2].isdigit()
        name_split = name.split(".")
        layer_idx = int(name_split[2])
        name_suffix = name_split[3:]
        pipe_name = ".".join([str(layer_idx + 2)] + name_suffix)

    return pipe_name

def _name_to_hf(name):
    """
    Convert pipeline model's name to normal model.

    Examples: 15.ln_1.bias -> transformer.h.15.ln_1.bias
    """
    name_split = name.split(".")
    parts = pipline_parts()
    layer_pipe_idx = int(name_split[0])
    if layer_pipe_idx == 0:
        # 0 -> embedding
        # 1 -> dropout
        hf_name = 'transformer.wte.weight'
    elif layer_pipe_idx == parts[-1] - 2:
        # one before last -> LayerNorm ln_f
        param_type = name_split[-1] # weight or bias
        hf_name = 'transformer.ln_f.' + param_type
    elif layer_pipe_idx == parts[-1] - 1:
        # last -> Linear lm_head
        param_type = name_split[-1] # weight or bias
        hf_name = 'lm_head.' + param_type
    else:
        # blocks
        block_idx = layer_pipe_idx - 2
        # 15.ln_1.bias -> transformer.h.15.ln_1.bias
        attr_list = ['transformer', 'h', str(block_idx)] + name_split[1:]
        hf_name = '.'.join(attr_list)

    return hf_name

def _weight_name_in_current_rank(names):
    if not is_pipeline():
        return names
    layers = pipline_layers_idx()
    parts = pipline_parts()
    cur_names = []
    # MossModel 的模型顺序为：
    # vocab: transformer.wte.weight
    # dropout: not in dict
    # MossBlock: transformer.h.{idx}.xxx
    # layernorm transformer.ln_f.xxx
    # linear: lm_head.xxx
    for name in names:
        # 找到 MossBlock。idx 对应到 layers_idx 需要 +2
        if len(name.split(".")) > 2 and name.split(".")[2].isdigit() \
            and (int(name.split(".")[2]) + 2) in layers:
                cur_names.append(name)
        if 0 in layers and name.startswith("transformer.wte."):
            # 0 层，embedding
            cur_names.append(name)
        if max(parts) - 1 in layers and name.startswith("lm_head."):
            # 最后一个，lm_head
            cur_names.append(name)
        if max(parts) - 2 in layers and name.startswith("transformer.ln_f."):
            # 倒数第二个 layer norm
            cur_names.append(name)

    return cur_names

def _rearrange_state_dict(state_dict, tp_rank, tp_size, process_exclusion):
    # 模型并行下进行 shape 匹配
    for name in list(state_dict.keys()):
        param = state_dict[name]
        if name.endswith("wte.weight"):
            # ParallelVocab
            chunk_dim = 0
        elif name.endswith("mlp.fc_out.weight") or \
            name.endswith("attn.out_proj.weight"):
                # RowParallelLinear
                chunk_dim = 1
        elif name.endswith("attn.qkv_proj.weight") or \
            "mlp.fc_in" in name or "lm_head" in name:
                # ColumnParallelLinear
                chunk_dim = 0
        else:
             continue

        tensor = list(torch.chunk(param, tp_size, dim=chunk_dim))[tp_rank].detach().clone()

        del state_dict[name]
        if process_exclusion:
            # CPU 内存回收（速度很慢）
            gc.collect()
        state_dict[name] = tensor
    # 流水线情况下，弹出不需要的并且更名
    if is_pipeline():
        cur_names = _weight_name_in_current_rank(state_dict.keys())
        for name in list(state_dict.keys()):
            if name in cur_names:
                state_dict[_name_to_pipline(name)] = state_dict[name]
            state_dict.pop(name)

    return state_dict
