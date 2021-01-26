# AUTOGENERATED! DO NOT EDIT! File to edit: notebooks/06_Sparse.ipynb (unless otherwise specified).

__all__ = ['device', 'Mv_scan_py', 'logZ_scan_py', 'ctc_loss_scan_py', 'Mv_scan_cupy', 'logZ_scan', 'cupy_funcs',
           'ctc_loss_scan', 'cupy_func', 'logZ_fwd_cupy', 'fwd_scores_cupy', 'bwd_scores_cupy', 'logZ', 'logZ_bwd_cupy',
           'ctc_loss']

# Cell
from functools import partial, lru_cache as cache
import numpy as np
import cupy as cp
import torch

from .core import semiring, Max, Log
from .utils import *
from .ctc import interleave_blanks, generate_sample_inputs, loss_pytorch, benchmark_fwd_bwd, report, compare_fwd_bwd

device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

# Cell
def Mv_scan_py(Ms, idx, v0, S:semiring=Log):
    T, N, C, nz = Ms.shape
    alpha = Ms.new_full((T+1, N, C), S.zero)
    alpha[0] = v0
    for t in range(T):
        alpha[t+1] = S.sum(S.mul(Ms[t], alpha[t, :, idx]), dim=2)
    return alpha

class _LogZ_scan(torch.autograd.Function):
    @staticmethod
    def forward(ctx, Ms, idx, v0, vT, S:semiring, scan):
        alpha = scan(Ms, idx, v0, S)
        ctx.save_for_backward(alpha, Ms, idx, vT)
        ctx.semiring, ctx.scan = S, scan
        return S.sum(S.mul(alpha[-1], vT), dim=1)

    @staticmethod
    def backward(ctx, grad):
        alpha, Ms, idx, vT = ctx.saved_tensors
        S, scan = ctx.semiring, ctx.scan
        T, N, C, nz = Ms.shape
        idx_T = idx.flatten().argsort().reshape(*idx.shape) #transpose
        Ms_T = Ms.reshape(T, N, -1)[:, :, idx_T]
        beta = scan(Ms_T.flip(0), idx_T // nz, vT, S)
        g = S.mul(S.mul(Ms.reshape(T, N, -1), alpha[:-1, :, idx.flatten()]).reshape(T, N, C, nz), beta[:-1, :, :, None].flip(0))
        g = S.dsum(g.reshape(T, N, -1), dim=2).reshape(T, N, C, nz)
        return grad[None, :, None, None] * g, None, None, None, None, None

def logZ_scan_py(Ms, idx, v0, vT, S:semiring):
    return _LogZ_scan.apply(Ms, idx, v0, vT, S, Mv_scan_py)

# Cell
from torch.nn.functional import pad

def _ctc_loss(logits, targets, input_lengths, target_lengths, logZ_impl, S:semiring=Log):
    zero, one = [logits.new_full((1,), x) for x in (S.zero, S.one)]
    scores = logits.log_softmax(2)
    states = interleave_blanks(targets, blank_idx=0)
    state_scores = torch.gather(scores, 2, states.expand(scores.size(0), -1, -1))
    final_states = torch.stack([target_lengths*2-1, target_lengths*2], 1)

    T, N, Lp = state_scores.shape
    assert torch.all(input_lengths == T)

    Ms = torch.stack([
        state_scores,
        pad(state_scores[:, :, 1:], (1, 0), value=S.zero),
        pad(torch.where(states[:, 2:] == states[:, :-2], zero.expand(T, N, Lp-2), state_scores[:, :, 2:]), (2, 0), value=S.zero)
    ], -1)

    i = torch.arange(Lp, device=device)
    rot = lambda x, n: torch.cat([x[-n:], x[:-n]])
    idx = torch.stack([i, rot(i, 1), rot(i, 2)], dim=1)

    v0 = torch.cat([one.expand(N, 1), zero.expand(N, Lp - 1)], dim=1)
    vT = zero.expand(N, Lp).clone().scatter_(1, final_states, S.one)

    logZ = logZ_impl(Ms, idx, v0, vT, S)
    return -(logZ / target_lengths).mean()

ctc_loss_scan_py = partial(_ctc_loss, logZ_impl=logZ_scan_py)

# Cell
cupy_funcs = {
    (torch.float32, Log): load_cupy_func('cuda/sparse_scan.cu', 'sparse_Mv_scan', FLOAT='float',  ADD='logsumexp2', MUL='add', ZERO='{:E}'.format(Log.zero)),
    (torch.float64, Log): load_cupy_func('cuda/sparse_scan.cu', 'sparse_Mv_scan', FLOAT='double',  ADD='logsumexp2', MUL='add', ZERO='{:E}'.format(Log.zero)),
    (torch.float32, Max): load_cupy_func('cuda/sparse_scan.cu', 'sparse_Mv_scan', FLOAT='float',  ADD='max2', MUL='add', ZERO='{:E}'.format(Log.zero)),
    (torch.float64, Max): load_cupy_func('cuda/sparse_scan.cu', 'sparse_Mv_scan', FLOAT='double',  ADD='max2', MUL='add', ZERO='{:E}'.format(Log.zero)),
}

def Mv_scan_cupy(Ms, idx, v0, S:semiring):
    T, N, C, nz = Ms.shape
    assert idx.shape == (C, nz)
    alpha = Ms.new_full((T+1, N, C), S.zero)
    alpha[0] = v0
    with cp.cuda.Device(Ms.device.index):
        cupy_funcs[(Ms.dtype, S)](grid=(N, 1, 1), block=(C, 1, 1), shared_mem=2*8*C,
               args=(alpha.data_ptr(), Ms.data_ptr(), idx.to(dtype=torch.int, device=Ms.device).data_ptr(), T, N, C, nz))
    return alpha

def logZ_scan(Ms, idx, v0, vT, S:semiring):
    return _LogZ_scan.apply(Ms, idx, v0, vT, S, Mv_scan_cupy)

ctc_loss_scan = partial(_ctc_loss, logZ_impl=logZ_scan)

# Cell
@cache(None)
def cupy_func(func_name, dtype, S, NZ, K):
    float_types = {torch.float32: 'float', torch.float64: 'double'}
    ops = {
        Log: {'sum': 'logsumexp', 'mul': 'add'},
        Max: {'sum': 'max_', 'mul': 'add'},
    }
    fname = 'cuda/sparse_logZ.cu'
    return load_cupy_func(fname, func_name, FLOAT=float_types[dtype],  MUL=ops[S]['mul'], ZERO='{:E}'.format(S.zero), SUM=ops[S]['sum'], NZ=NZ, K=K)

def logZ_fwd_cupy(Ms, idx, v0, vT, S:semiring=Log, K=4):
    assert Ms.device.index is not None
    T, N, C, NZ = Ms.shape
    assert idx.shape == (C, NZ)
    idx = idx.to(dtype=torch.int, device=Ms.device)
    Ms_grad = Ms.new_full((T, N, C, NZ), S.zero)
    logZ = Ms.new_full((N, C), S.zero)
    _bytes = 8 if (Ms.dtype == torch.float64) else 4
    with cp.cuda.Device(Ms.device.index):
        cupy_func('logZ_fwd', Ms.dtype, S, NZ, K)(grid=(N, 1, 1), block=(C//K, 1, 1), shared_mem=2*_bytes*C,
               args=(logZ.data_ptr(), Ms_grad.data_ptr(), Ms.data_ptr(), v0.data_ptr(), vT.data_ptr(), idx.data_ptr(), T, N, C))
    return S.sum(logZ, dim=1), Ms_grad

def fwd_scores_cupy(Ms, idx, v0, S:semiring=Log, K=4):
    T, N, C, NZ = Ms.shape
    alphas = Ms.new_full((T+1, N, C), S.zero)
    idx = idx.to(dtype=torch.int, device=Ms.device)
    _bytes = 8 if (Ms.dtype == torch.float64) else 4
    with cp.cuda.Device(Ms.device.index):
        cupy_func('fwd_scores', Ms.dtype, S, NZ, K)(grid=(N, 1, 1), block=(C//K, 1, 1), shared_mem=2*_bytes*C,
               args=(alphas.data_ptr(), Ms.data_ptr(), v0.data_ptr(), idx.data_ptr(), T, N, C))
    return alphas

def bwd_scores_cupy(Ms, idx, vT, S:semiring=Log, K=4):
    T, N, C, NZ = Ms.shape
    betas = Ms.new_full((T+1, N, C), S.zero)
    idx_T = idx.flatten().argsort().to(dtype=torch.int, device=Ms.device) #transpose
    _bytes = 8 if (Ms.dtype == torch.float64) else 4
    with cp.cuda.Device(Ms.device.index):
        cupy_func('bwd_scores', Ms.dtype, S, NZ, K)(grid=(N, 1, 1), block=(C//K, 1, 1), shared_mem=2*_bytes*C,
               args=(betas.data_ptr(), Ms.data_ptr(), vT.data_ptr(), idx_T.data_ptr(), T, N, C))
    return betas

logZ_bwd_cupy = bwd_scores_cupy #backward compatibility for renamed function

class _LogZ(torch.autograd.Function):
    @staticmethod
    def forward(ctx, Ms, idx, v0, vT, S:semiring, K):
        idx = idx.to(device=Ms.device)
        logZ, Ms_grad = logZ_fwd_cupy(Ms, idx, v0, vT, S, K)
        ctx.save_for_backward(Ms_grad, Ms, idx, vT)
        ctx.semiring = S
        ctx.K = K
        return logZ

    @staticmethod
    def backward(ctx, grad):
        Ms_grad, Ms, idx, vT = ctx.saved_tensors
        S, K = ctx.semiring, ctx.K
        T, N, C, NZ = Ms.shape
        betas = bwd_scores_cupy(Ms, idx, vT, S, K=K)
        Ms_grad = S.mul(Ms_grad, betas[1:,:,:,None])
        Ms_grad = S.dsum(Ms_grad.reshape(T, N, -1), dim=2).reshape(T, N, C, NZ)
        return grad[None, :, None, None] * Ms_grad, None, None, None, None, None

def logZ(Ms, idx, v0, vT, S:semiring=Log, K=1):
    return _LogZ.apply(Ms, idx, v0, vT, S, K)

ctc_loss = partial(_ctc_loss, logZ_impl=logZ)