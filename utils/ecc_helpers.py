from collections import defaultdict
from functools import partial
import jax
from jax import lax
from jax._src.typing import Array
from jax.experimental.sparse import BCOO
import jax.numpy as jnp
import numba as nb
import numpy as np
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import laplacian
from typing import Any, Tuple, List

from solver.utils import apply_A_operator_batched
from utils.common import (SDPState,
                          scale_sdp_state,
                          unscale_sdp_state,
                          reconstruct_from_sketch,
                          apply_A_operator_mx)

from IPython import embed


def get_all_problem_data(C: BCOO) -> Tuple[BCOO, Array, Array, Array]:
    n = C.shape[0]

    # constraint: diagonal of X is all 1's
    range_n = jnp.arange(n)[:, None]
    A_indices = jnp.hstack(3*[range_n])
    A_data = jnp.ones((n,))
    b = jnp.ones((n,))
    b_ineq_mask = jnp.zeros((n,))

    # constraint: objective-relevant entries of X >= 0, written as -X <= 0
    triu_indices_mask = (C.indices[:, 0] <= C.indices[:, 1])
    constraint_indices = b.shape[0] + jnp.arange(jnp.sum(triu_indices_mask))
    constraint_triples = jnp.concatenate(
        [constraint_indices[:, None], C.indices[triu_indices_mask]], axis=1)
    constraint_triples = jnp.concatenate(
        [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
    A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
    A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), -0.5)], axis=0)
    b = jnp.concatenate([b, jnp.full((constraint_indices.shape[0],), 0.0)], axis=0)
    b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((constraint_indices.shape[0],), 1.0)], axis=0)

    return A_data, A_indices, b, b_ineq_mask


def initialize_state(C: BCOO, sketch_dim: int) -> SDPState:
    A_data, A_indices, b, b_ineq_mask = get_all_problem_data(C)
    n = C.shape[0]
    m = b.shape[0]

    SCALE_X = 1.0 / float(n)
    SCALE_C = 1.0 / jnp.linalg.norm(C.data)  # equivalent to frobenius norm
    SCALE_A = jnp.ones_like(b)

    if sketch_dim == -1:
        X = jnp.zeros((n, n))
        Omega = None
        P = None
    elif sketch_dim > 0:
        X = None
        Omega = jax.random.normal(jax.random.PRNGKey(0), shape=(n, sketch_dim))
        P = jnp.zeros_like(Omega)
    else:
        raise ValueError("Invalid value for sketch_dim")

    y = jnp.zeros((m,))
    z = jnp.zeros((m,))
    tr_X = jnp.array(0.0)
    primal_obj = jnp.array(0.0)

    sdp_state = SDPState(
        C=C,
        A_indices=A_indices,
        A_data=A_data,
        b=b,
        b_ineq_mask=b_ineq_mask,
        X=X,
        P=P,
        Omega=Omega,
        y=y,
        z=z,
        tr_X=tr_X,
        primal_obj=primal_obj,
        SCALE_C=SCALE_C,
        SCALE_X=SCALE_X,
        SCALE_A=SCALE_A)

    print("SCALE_C: ", SCALE_C)
    print("SCALE_X: ", SCALE_X)
    print("min(SCALE_A): ", jnp.min(SCALE_A))
    print("max(SCALE_A): ", jnp.max(SCALE_A))

    sdp_state = scale_sdp_state(sdp_state)
    return sdp_state


def cold_start_add_constraint(
    old_sdp_state: SDPState,
    ortho_indices: List[Tuple[int, int]],
    sum_gt_one_constraints: List[List[int]],
    sketch_dim: int) -> SDPState:

    old_sdp_state = unscale_sdp_state(old_sdp_state)

    old_n = old_sdp_state.C.shape[0]
    n = old_n + 1
    C = BCOO((old_sdp_state.C.data, old_sdp_state.C.indices), shape=(n, n))

    # add the additional diagonal == 1 constraint for the new ecc
    A_indices = jnp.concatenate([old_sdp_state.A_indices,
                                 jnp.array([[old_sdp_state.b.shape[0], n-1, n-1]])], axis=0)
    A_data = jnp.concatenate([old_sdp_state.A_data, jnp.array([1.0])], axis=0)
    b = jnp.concatenate([old_sdp_state.b, jnp.array([1.0])], axis=0)
    b_ineq_mask = jnp.concatenate([old_sdp_state.b_ineq_mask, jnp.array([0.0])], axis=0)

    # add ortho indices constraints
    if len(ortho_indices) > 0:
        num_ortho_indices = len(ortho_indices)
        constraint_triples = jnp.array([[b.shape[0] + i, u, v]
                                        for i, (u, v) in enumerate(ortho_indices)])
        constraint_triples = jnp.concatenate(
            [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
        A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
        A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), 1.0)], axis=0)
        b = jnp.concatenate([b, jnp.full((num_ortho_indices,), 0.0)], axis=0)
        b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((num_ortho_indices,), 1.0)], axis=0)

    # add sum greater than one (feature satisfying hyperplanes) constraints
    num_hyperplanes = len(sum_gt_one_constraints)
    constraint_triples = jnp.array([(b.shape[0] + i, u, v)
                                    for i, pairs in enumerate(sum_gt_one_constraints)
                                    for u, v in pairs])
    constraint_triples = jnp.concatenate(
        [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
    A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
    A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), -0.5)], axis=0)
    b = jnp.concatenate([b, jnp.full((num_hyperplanes,), -1.0)], axis=0)
    b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((num_hyperplanes,), 1.0)], axis=0)

    # add constraints for forcing to 1
    mixed_var_tuples = [(u, v, pair_idx) for pair_idx, pairs in enumerate(sum_gt_one_constraints)
                          for u, v in pairs if len(pairs) > 1]
    mixed_var_map = defaultdict(list)
    for i, (_, _, pair_idx) in enumerate(mixed_var_tuples):
        mixed_var_map[pair_idx].append(b.shape[0] + i)

    constraint_triples = jnp.array(
        [(b.shape[0] + i, u, v) for i, (u, v, _) in enumerate(mixed_var_tuples)])
    constraint_triples = jnp.concatenate(
        [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
    A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
    A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), -0.5)], axis=0)
    b = jnp.concatenate([b, jnp.full((num_hyperplanes,), 0.0)], axis=0) # change this to -1.0 when we want to force
    b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((num_hyperplanes,), 1.0)], axis=0)

    m = b.shape[0]

    if sketch_dim == -1:
        X = jnp.zeros((n, n))
        Omega = None
        P = None
    elif sketch_dim > 0:
        X = None
        Omega = jax.random.normal(jax.random.PRNGKey(0), shape=(n, sketch_dim))
        P = jnp.zeros_like(Omega)
    else:
        raise ValueError("Invalid value for sketch_dim")

    y = jnp.zeros((m,))
    z = jnp.zeros((m,))
    tr_X = jnp.array(0.0)
    primal_obj = jnp.array(0.0)

    SCALE_X = 1.0 / float(n)
    SCALE_C = 1.0 / jnp.linalg.norm(C.data)  # equivalent to frobenius norm
    SCALE_A = jnp.ones_like(b)

    sdp_state = SDPState(
        C=C,
        A_indices=A_indices,
        A_data=A_data,
        b=b,
        b_ineq_mask=b_ineq_mask,
        X=X,
        P=P,
        Omega=Omega,
        y=y,
        z=z,
        tr_X=tr_X,
        primal_obj=primal_obj,
        SCALE_C=SCALE_C,
        SCALE_X=SCALE_X,
        SCALE_A=SCALE_A)

    print("SCALE_C: ", SCALE_C)
    print("SCALE_X: ", SCALE_X)
    print("min(SCALE_A): ", jnp.min(SCALE_A))
    print("max(SCALE_A): ", jnp.max(SCALE_A))

    sdp_state = scale_sdp_state(sdp_state)
    return sdp_state, mixed_var_map


def warm_start_add_constraint(
    old_sdp_state: SDPState,
    ortho_indices: List[Tuple[int, int]],
    sum_gt_one_constraints: List[List[int]],
    prev_pred_clusters: Array,
    constraint_scale_factor: float,
    sketch_dim: int) -> SDPState:

    old_sdp_state = unscale_sdp_state(old_sdp_state)

    old_n = old_sdp_state.C.shape[0]
    n = old_n + 1
    C = BCOO((old_sdp_state.C.data, old_sdp_state.C.indices), shape=(n, n))

    # add the additional diagonal == 1 constraint for the new ecc
    A_indices = jnp.concatenate([old_sdp_state.A_indices,
                                 jnp.array([[old_sdp_state.b.shape[0], n-1, n-1]])], axis=0)
    A_data = jnp.concatenate([old_sdp_state.A_data, jnp.array([1.0])], axis=0)
    b = jnp.concatenate([old_sdp_state.b, jnp.array([1.0])], axis=0)
    b_ineq_mask = jnp.concatenate([old_sdp_state.b_ineq_mask, jnp.array([0.0])], axis=0)

    # add ortho indices constraints
    if len(ortho_indices) > 0:
        num_ortho_indices = len(ortho_indices)
        constraint_triples = jnp.array([[b.shape[0] + i, u, v]
                                        for i, (u, v) in enumerate(ortho_indices)])
        constraint_triples = jnp.concatenate(
            [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
        A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
        A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), 1.0)], axis=0)
        b = jnp.concatenate([b, jnp.full((num_ortho_indices,), 0.0)], axis=0)
        b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((num_ortho_indices,), 1.0)], axis=0)

    # add sum greater than one (feature satisfying hyperplanes) constraints
    num_hyperplanes = len(sum_gt_one_constraints)
    constraint_triples = jnp.array([(b.shape[0] + i, u, v)
                                    for i, pairs in enumerate(sum_gt_one_constraints)
                                    for u, v in pairs])
    constraint_triples = jnp.concatenate(
        [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
    A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
    A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), -0.5)], axis=0)
    b = jnp.concatenate([b, jnp.full((num_hyperplanes,), -1.0)], axis=0)
    b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((num_hyperplanes,), 1.0)], axis=0)

    # add constraints for forcing to 1
    mixed_var_tuples = [(u, v, pair_idx) for pair_idx, pairs in enumerate(sum_gt_one_constraints)
                          for u, v in pairs if len(pairs) > 1]
    mixed_var_map = defaultdict(list)
    for i, (_, _, pair_idx) in enumerate(mixed_var_tuples):
        mixed_var_map[pair_idx].append(b.shape[0] + i)

    constraint_triples = jnp.array(
        [(b.shape[0] + i, u, v) for i, (u, v, _) in enumerate(mixed_var_tuples)])
    constraint_triples = jnp.concatenate(
        [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
    A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
    A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), -0.5)], axis=0)
    b = jnp.concatenate([b, jnp.full((num_hyperplanes,), 0.0)], axis=0) # change this to -1.0 when we want to force
    b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((num_hyperplanes,), 1.0)], axis=0)

    m = b.shape[0]

    ecc_points_and_counts = [(pairs[1], len(l)) for l in sum_gt_one_constraints for pairs in l]
    ecc_points_and_counts = jnp.array(list(set(ecc_points_and_counts)))
    ecc_points = ecc_points_and_counts[:, 0]
    ecc_counts = ecc_points_and_counts[:, 1]

    neg_points = jnp.array([v for v, _ in ortho_indices])

    embed_dim = max(jnp.unique(prev_pred_clusters).shape[0], 2)

    X = old_sdp_state.X
    Omega = old_sdp_state.Omega
    P = old_sdp_state.P
    if old_sdp_state.X is not None:
        eigvals, eigvecs = jnp.linalg.eigh(old_sdp_state.X)
        point_embeds = (eigvecs[:,-embed_dim:] * jnp.sqrt(eigvals[None, -embed_dim:]))
        point_embeds = point_embeds / jnp.linalg.norm(point_embeds, axis=1)[:, None]
        avg_embed = jnp.sum(point_embeds[ecc_points] / ecc_counts[:, None], axis=0)
        avg_embed = avg_embed / jnp.linalg.norm(avg_embed)
        point_embeds = jnp.concatenate([point_embeds, avg_embed[None, :]], axis=0)
        if neg_points.size > 0:
            point_embeds = point_embeds.at[neg_points].set(jnp.zeros_like(point_embeds[0]))
        X = point_embeds @ point_embeds.T
        z = apply_A_operator_mx(n, m, A_data, A_indices, X) 
        tr_X = jnp.trace(X)
        primal_obj = jnp.trace(C @ X)
    if old_sdp_state.P is not None:
        Omega = jax.random.normal(jax.random.PRNGKey(n), shape=(n, sketch_dim))
        E, Lambda = reconstruct_from_sketch(old_sdp_state.Omega, old_sdp_state.P)
        tr_offset = (old_sdp_state.tr_X - jnp.sum(Lambda)) / Lambda.shape[0]
        Lambda_tr_correct = Lambda + tr_offset
        point_embeds = E * jnp.sqrt(Lambda_tr_correct)[None, :]
        point_embeds = point_embeds / jnp.linalg.norm(point_embeds, axis=1)[:, None]
        avg_embed = jnp.sum(point_embeds[ecc_points] / ecc_counts[:, None], axis=0)
        avg_embed = avg_embed / jnp.linalg.norm(avg_embed)
        point_embeds = jnp.concatenate([point_embeds, avg_embed[None, :]], axis=0)
        if neg_points.size > 0:
            point_embeds = point_embeds.at[neg_points].set(jnp.zeros_like(point_embeds[0]))
        P = point_embeds @ (point_embeds.T @ Omega)
        z = apply_A_operator_batched(m, A_data, A_indices, point_embeds)
        tr_X = jnp.sum(Lambda_tr_correct)
        primal_obj = jnp.trace(point_embeds.T @ (C @ point_embeds))

    SCALE_X = 1.0 / float(n)
    SCALE_C = 1.0 / jnp.linalg.norm(C.data)  # equivalent to Frobenius norm
    SCALE_A = jnp.full(b.shape, constraint_scale_factor).at[jnp.arange(old_sdp_state.b.shape[0])].set(1.0)
    SCALE_A = SCALE_A.at[ecc_points].set(constraint_scale_factor)
    if neg_points.size > 0:
        SCALE_A = SCALE_A.at[neg_points].set(constraint_scale_factor)

    y = jnp.zeros((m,))
    y = y.at[jnp.arange(old_sdp_state.b.shape[0])].set(old_sdp_state.y)
    y = y * (SCALE_X / old_sdp_state.SCALE_X)

    sdp_state = SDPState(
        C=C,
        A_indices=A_indices,
        A_data=A_data,
        b=b,
        b_ineq_mask=b_ineq_mask,
        X=X,
        P=P,
        Omega=Omega,
        y=y,
        z=z,
        tr_X=tr_X,
        primal_obj=primal_obj,
        SCALE_C=SCALE_C,
        SCALE_X=SCALE_X,
        SCALE_A=SCALE_A)

    print("SCALE_C: ", SCALE_C)
    print("SCALE_X: ", SCALE_X)
    print("min(SCALE_A): ", jnp.min(SCALE_A))
    print("max(SCALE_A): ", jnp.max(SCALE_A))

    sdp_state = scale_sdp_state(sdp_state)
    return sdp_state, mixed_var_map


def create_sparse_laplacian(edge_weights: coo_matrix, eps: float) -> csr_matrix:
    pos_mask = (edge_weights.data > 0)
    pos_graph = coo_matrix(
        (edge_weights.data[pos_mask],
            (edge_weights.row[pos_mask], edge_weights.col[pos_mask])),
        shape=edge_weights.shape)
    neg_graph = coo_matrix(
        (-edge_weights.data[~pos_mask],
            (edge_weights.row[~pos_mask], edge_weights.col[~pos_mask])),
        shape=edge_weights.shape)

    pos_n, pos_m = pos_graph.shape[0], pos_graph.data.shape[0]
    neg_n, neg_m = neg_graph.shape[0], neg_graph.data.shape[0]

    pos_k = np.ceil(np.log(pos_n) / eps**2).astype(int)
    neg_k = np.ceil(np.log(neg_n) / eps**2).astype(int)

    pos_diag_edge_mx = coo_matrix(
        (pos_graph.data, (np.arange(pos_m), np.arange(pos_m))), shape=(pos_m, pos_m))
    neg_diag_edge_mx = coo_matrix(
        (neg_graph.data, (np.arange(neg_m), np.arange(neg_m))), shape=(neg_m, neg_m))

    pos_incidence_mx = coo_matrix(
        (np.concatenate([np.ones((pos_m,)), np.full((pos_m,), -1.0)]),
            (np.concatenate([np.arange(pos_m), np.arange(pos_m)]),
            np.concatenate([pos_graph.row, pos_graph.col]))),
        shape=(pos_m, pos_n))
    neg_incidence_mx = coo_matrix(
        (np.concatenate([np.ones((neg_m,)), np.full((neg_m,), -1.0)]),
            (np.concatenate([np.arange(neg_m), np.arange(neg_m)]),
            np.concatenate([neg_graph.row, neg_graph.col]))),
        shape=(neg_m, neg_n))

    pos_laplacian_mx = pos_incidence_mx.T @ pos_diag_edge_mx @ pos_incidence_mx 
    neg_laplacian_mx = neg_incidence_mx.T @ neg_diag_edge_mx @ neg_incidence_mx 

    # if the matrix is small, we do not need to sample
    if pos_laplacian_mx.shape[0] < 15:
        return pos_laplacian_mx - neg_laplacian_mx

    pos_rand_proj = np.random.binomial(n=1, p=0.5, size=(pos_m, pos_k))
    pos_rand_proj = (1 / np.sqrt(pos_k)) * (2 * pos_rand_proj - 1)
    neg_rand_proj = np.random.binomial(n=1, p=0.5, size=(neg_m, neg_k))
    neg_rand_proj = (1 / np.sqrt(neg_k)) * (2 * neg_rand_proj - 1)

    pos_resistance_embeds = (pos_rand_proj.T
                                @ np.sqrt(pos_diag_edge_mx)
                                @ pos_incidence_mx
                                @ np.linalg.pinv(pos_laplacian_mx.todense())).T 
    neg_resistance_embeds = (neg_rand_proj.T
                                @ np.sqrt(neg_diag_edge_mx)
                                @ neg_incidence_mx
                                @ np.linalg.pinv(neg_laplacian_mx.todense())).T 

    pos_resistances = np.linalg.norm(pos_incidence_mx @ pos_resistance_embeds, axis=1)**2
    neg_resistances = np.linalg.norm(neg_incidence_mx @ neg_resistance_embeds, axis=1)**2

    pos_energies = pos_diag_edge_mx @ pos_resistances
    neg_energies = neg_diag_edge_mx @ neg_resistances

    pos_probs = pos_energies / np.sum(pos_energies)
    neg_probs = neg_energies / np.sum(neg_energies)

    num_sample_edges = np.ceil(pos_n * np.log(pos_n) / (2 * eps ** 2)).astype(int)

    if num_sample_edges < pos_m:
        sampled_edges = np.random.multinomial(num_sample_edges, pos_probs, size=1)
        sampled_edge_weights = (sampled_edges @ pos_diag_edge_mx) / (num_sample_edges * pos_probs)
        sampled_edge_weights = sampled_edge_weights.squeeze()
        sampled_edge_mask = sampled_edge_weights != 0.0
        sampled_pos_graph = coo_matrix(
            (pos_graph.data[sampled_edge_mask],
            (pos_graph.row[sampled_edge_mask], pos_graph.col[sampled_edge_mask])),
            shape=pos_graph.shape)
        sampled_pos_graph = sampled_pos_graph + sampled_pos_graph.T
        sampled_pos_laplacian = laplacian(sampled_pos_graph)
    else:
        sampled_pos_laplacian = pos_laplacian_mx

    if num_sample_edges < neg_m:
        sampled_edges = np.random.multinomial(num_sample_edges, neg_probs, size=1)
        sampled_edge_weights = (sampled_edges @ neg_diag_edge_mx) / (num_sample_edges * neg_probs)
        sampled_edge_weights = sampled_edge_weights.squeeze()
        sampled_edge_mask = sampled_edge_weights != 0.0
        sampled_neg_graph = coo_matrix(
            (neg_graph.data[sampled_edge_mask],
            (neg_graph.row[sampled_edge_mask], neg_graph.col[sampled_edge_mask])),
            shape=neg_graph.shape)
        sampled_neg_graph = sampled_neg_graph + sampled_neg_graph.T
        sampled_neg_laplacian = laplacian(sampled_neg_graph)
    else:
        sampled_neg_laplacian = neg_laplacian_mx

    sparse_laplacian = sampled_pos_laplacian - sampled_neg_laplacian

    return sparse_laplacian