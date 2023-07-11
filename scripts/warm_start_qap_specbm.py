import argparse
import jax
from jax.experimental.sparse import BCOO
import jax.numpy as jnp
import json
import numba as nb
import numpy as np
import pickle

from solver.specbm import specbm
from misc.qap_helpers import (load_and_process_qap,
                              load_and_process_tsp,
                              get_all_problem_data,
                              qap_round)

from IPython import embed


@nb.njit
def fill_constraint_index_map(old_A_indices, new_A_indices, constraint_index_map) -> None:
    old_idx = 0
    for curr_idx in range(new_A_indices.shape[0]):
        old_row = old_A_indices[old_idx]
        curr_row = new_A_indices[curr_idx]
        if curr_row[1] == old_row[1] and curr_row[2] == old_row[2]:
            constraint_index_map[old_row[0]] = curr_row[0]
            old_idx += 1


def get_hparams():
    parser = argparse.ArgumentParser() 
    parser.add_argument("--data_path", type=str, required=True, help="path to mat file")
    parser.add_argument("--max_iters", type=int, required=True,
                        help="number of iterations to run solver")
    parser.add_argument("--k_curr", type=int, default=1,
                        help="number of new eigenvectors to compute")
    parser.add_argument("--k_past", type=int, default=0,
                        help="number of new eigenvectors to compute")
    parser.add_argument("--trace_factor", type=float, default=1.0,
                        help="how much space to give trace")
    parser.add_argument("--rho", type=float, default=0.1,
                        help="proximal parameter")
    parser.add_argument("--beta", type=float, default=0.25,
                        help="sufficient decrease parameter")
    hparams = parser.parse_args()
    return hparams


if __name__ == "__main__":
    jax.config.update("jax_enable_x64", True)

    # get experiment hparams and print them out
    hparams = get_hparams()
    print(json.dumps(vars(hparams), indent=4))

    #DATAFILE = "data/qap/qapdata/chr12a.dat"
    #DATAFILE = "data/qap/tspdata/ulysses16.tsp"
    #DATAFILE = "data/qap/tspdata/dantzig42.tsp"
    #DATAFILE = "data/qap/tspdata/bayg29.tsp"
    #DATAFILE = "data/qap/tspdata/bays29.tsp"
    #DATAFILE = "data/qap/tspdata/att48.tsp"

    # for warm-start, TODO: turn this into an input parameter
    num_drop = 1

    DATAFILE = hparams.data_path
    if DATAFILE.split(".")[-1] == "dat":
        l, D, W, C = load_and_process_qap(DATAFILE, num_drop=num_drop)
    elif DATAFILE.split(".")[-1] == "tsp":
        l, D, W, C = load_and_process_tsp(DATAFILE, num_drop=num_drop)
    else:
        raise ValueError("Invalid data file type.")

    A_indices, A_data, b, b_ineq_mask = get_all_problem_data(C)
    n = C.shape[0]
    m = b.shape[0]

    SCALE_X = 1.0 / float(l + 1)
    SCALE_C = 1.0 / jnp.linalg.norm(C.data)  # equivalent to frobenius norm
    SCALE_A = jnp.zeros((m,))
    SCALE_A = SCALE_A.at[A_indices[:,0]].add(A_data**2)
    SCALE_A = 1.0 / jnp.sqrt(SCALE_A)
    #SCALE_X = 1.0
    #SCALE_C = 1.0
    #SCALE_A = jnp.ones((m,))

    scaled_C = BCOO((C.data * SCALE_C, C.indices), shape=C.shape)
    scaled_b = b * SCALE_X * SCALE_A
    scaled_A_data = A_data * SCALE_A.at[A_indices[:,0]].get()

    #X = jnp.zeros((n, n))
    #Omega = None
    #P = None
    X = None
    Omega = jax.random.normal(jax.random.PRNGKey(0), shape=(n, l + num_drop))
    P = jnp.zeros_like(Omega)
    y = jnp.zeros((m,))
    z = jnp.zeros((m,))
    tr_X = 0.0
    primal_obj = 0.0

    trace_ub = hparams.trace_factor * float(l + 1) * SCALE_X

    k_curr = hparams.k_curr
    k_past = hparams.k_past

    callback_static_args = pickle.dumps({"l": l})
    callback_nonstatic_args = {"D": D, "W": W}

    X, P, y, z, primal_obj, tr_X = specbm(
        X=X,
        P=P,
        y=y,
        z=z,
        primal_obj=primal_obj,
        tr_X=tr_X,
        n=n,
        m=m,
        trace_ub=trace_ub,
        C=scaled_C,
        A_data=scaled_A_data,
        A_indices=A_indices,
        b=scaled_b,
        b_ineq_mask=b_ineq_mask,
        Omega=Omega,
        rho=hparams.rho,
        beta=hparams.beta,
        k_curr=k_curr,
        k_past=k_past,
        SCALE_C=SCALE_C,
        SCALE_X=SCALE_X,
        SCALE_A=SCALE_A,
        eps=1e-5,  # hparams.eps,
        max_iters=hparams.max_iters,  # hparams.max_iters,
        lanczos_inner_iterations=min(n, 32),
        lanczos_max_restarts=100,  # hparams.lanczos_max_restarts,
        subprob_eps=1e-7,
        subprob_max_iters=15,
        callback_fn=qap_round,
        callback_static_args=callback_static_args,
        callback_nonstatic_args=callback_nonstatic_args)

    old_A_indices = A_indices
    old_m = m

    DATAFILE = hparams.data_path
    if DATAFILE.split(".")[-1] == "dat":
        l, D, W, C = load_and_process_qap(DATAFILE, num_drop=0)
    elif DATAFILE.split(".")[-1] == "tsp":
        l, D, W, C = load_and_process_tsp(DATAFILE, num_drop=0)
    else:
        raise ValueError("Invalid data file type.")

    A_indices, A_data, b, b_ineq_mask = get_all_problem_data(C)
    n = C.shape[0]
    m = b.shape[0]

    # TODO: warm-start X, P, Omega, y, and z 
    # TODO: fix scaling
    reindex = lambda a : a + num_drop * (a // (l - num_drop))

    if X is not None:
        X = BCOO.fromdense(X)
        X = BCOO((X.data, jax.vmap(reindex)(X.indices)), shape=(n, n)).todense()
        X /= SCALE_X
    if P is not None:
        Omega = jax.random.normal(jax.random.PRNGKey(n), shape=(n, l)).at[jax.vmap(reindex)(
            jnp.arange(Omega.shape[0]))].set(Omega)
        P = jnp.zeros_like(Omega).at[jax.vmap(reindex)(jnp.arange(P.shape[0]))].set(P)
        P /= SCALE_X

    old_A_indices = old_A_indices.at[:, 1:].set(jax.vmap(reindex)(old_A_indices[:, 1:]))
    constraint_index_map = np.empty((old_m,), dtype=int)
    fill_constraint_index_map(
        np.asarray(old_A_indices), np.asarray(A_indices), constraint_index_map)

    y = jnp.zeros((m,)).at[constraint_index_map].set(y)
    z = jnp.zeros((m,)).at[constraint_index_map].set((z / SCALE_A) / SCALE_X)

    primal_obj /= SCALE_X * SCALE_C
    tr_X /= SCALE_X

    SCALE_X = 1.0 / float(l + 1)
    SCALE_C = 1.0 / jnp.linalg.norm(C.data)  # equivalent to frobenius norm
    SCALE_A = jnp.zeros((m,))
    SCALE_A = SCALE_A.at[A_indices[:,0]].add(A_data**2)
    SCALE_A = 1.0 / jnp.sqrt(SCALE_A)

    scaled_C = BCOO((C.data * SCALE_C, C.indices), shape=C.shape)
    scaled_b = b * SCALE_X * SCALE_A
    scaled_A_data = A_data * SCALE_A.at[A_indices[:,0]].get()

    # scale data with respect to new scale
    if X is not None:
        X *= SCALE_X
    if P is not None:
        P *= SCALE_X
    z *= SCALE_X
    primal_obj *= SCALE_C * SCALE_X
    tr_X *= SCALE_X

    trace_ub = hparams.trace_factor * float(l + 1) * SCALE_X

    k_curr = hparams.k_curr
    k_past = hparams.k_past

    callback_static_args = pickle.dumps({"l": l})
    callback_nonstatic_args = {"D": D, "W": W}

    X, P, y, z, primal_obj, tr_X = specbm(
        X=X,
        P=P,
        y=y,
        z=z,
        primal_obj=primal_obj,
        tr_X=tr_X,
        n=n,
        m=m,
        trace_ub=trace_ub,
        C=scaled_C,
        A_data=scaled_A_data,
        A_indices=A_indices,
        b=scaled_b,
        b_ineq_mask=b_ineq_mask,
        Omega=Omega,
        rho=hparams.rho,
        beta=hparams.beta,
        k_curr=k_curr,
        k_past=k_past,
        SCALE_C=SCALE_C,
        SCALE_X=SCALE_X,
        SCALE_A=SCALE_A,
        eps=1e-5,  # hparams.eps,
        max_iters=hparams.max_iters,  # hparams.max_iters,
        lanczos_inner_iterations=min(n, 32),
        lanczos_max_restarts=100,  # hparams.lanczos_max_restarts,
        subprob_eps=1e-7,
        subprob_max_iters=15,
        callback_fn=qap_round,
        callback_static_args=callback_static_args,
        callback_nonstatic_args=callback_nonstatic_args)