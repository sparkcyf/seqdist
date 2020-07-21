# AUTOGENERATED! DO NOT EDIT! File to edit: notebooks/04_Normalisation.ipynb (unless otherwise specified).

__all__ = ['device', 'generate_test_example', 'logZ_py', 'fused_batch_Mv', 'LogZ', 'cupy_funcs', 'logZ']

# Cell
import numpy as np
import cupy as cp
import torch
import torch.nn as nn
from .utils import *

device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

# Cell
def generate_test_example(T, N, n_state, dtype=torch.float):
    return torch.rand((T, N, n_state, n_state), device=device, dtype=dtype, requires_grad=True)

# Cell
import torch

def _rescale(M):
    #T, N, n_state, n_state = M.shape
    Z = M.sum((2, 3), keepdim=True) / M.size(3)
    logZ = torch.log(Z).sum(0).reshape(-1)
    return M / Z, logZ

@torch.jit.script
def logZ_py(M, alpha_0):
    M, logZ = _rescale(M)
    T, N, n_state, _ = M.shape
    alpha = alpha_0.unsqueeze(2)
    for i, M_t in enumerate(M.unbind(0)):
        alpha = M_t.bmm(alpha)
        if i % 32 == (T - 1) % 32:
            z = alpha.sum(1, keepdim=True)
            alpha = alpha/z
            logZ += torch.log(z.squeeze())
    return logZ

# Cell
from .ctc import Log

cupy_funcs = {
    (torch.float32, Log): load_cupy_func('cuda/fused_bmv.cu', 'fwd', FLOAT='float', MUL='add', SUM='logsumexp2'),
    (torch.float64, Log): load_cupy_func('cuda/fused_bmv.cu', 'fwd', FLOAT='double', MUL='add', SUM='logsumexp2'),
}

def fused_batch_Mv(Ms, alpha_0):
    T, N, n_state, _ = Ms.shape
    alpha = Ms.new_zeros((T + 1, N, n_state))
    alpha[0] = alpha_0
    with cp.cuda.Device(Ms.device.index):
        cupy_funcs[(Ms.dtype, Log)](
            grid=(N, 1, 1),
            block=(n_state, 1, 1),
            args=(alpha.data_ptr(), Ms.contiguous().data_ptr(), T, N, n_state)
        )
    return alpha

class LogZ(torch.autograd.Function):
    @staticmethod
    def forward(ctx, Ms, alpha_0, beta_T):
        alpha = fused_batch_Mv(Ms, alpha_0)
        ctx.save_for_backward(Ms, alpha, beta_T)
        return (alpha[-1] + beta_T).logsumexp(1)

    @staticmethod
    def backward(ctx, g):
        Ms, alpha, beta_T = ctx.saved_tensors
        T, N, n_state, _ = Ms.shape
        beta = fused_batch_Mv(Ms.transpose(2, 3).flip(0), beta_T)
        Ms_grad = Ms + alpha[:-1,:,None,:] + (beta[:-1, :, :, None]).flip(0)
        Ms_grad = torch.softmax(Ms_grad.reshape(T, N, -1), 2).reshape(T, N, n_state, n_state)
        return Ms_grad * g[None, :, None, None], None, None

logZ = LogZ.apply