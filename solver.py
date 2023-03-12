from collections import namedtuple
import jax
import jax.numpy as jnp
from jax import lax
from jax._src.typing import Array
import numpy as np
import scipy  # type: ignore
from scipy.linalg import eigh_tridiagonal  # type: ignore 
from typing import Any, Callable, Tuple

from IPython import embed


# TODO: implement storage efficient version?
def approx_k_min_eigen(
    M: Callable[[Array], Array],
    n: int,
    k: int,
    num_iters: int,
    eps: float,
    rng: Array
) -> Tuple[Array, Array]:

    TriDiagStateStruct = namedtuple(
        "TriDiagStateStruct", 
        ["t", "V", "diag", "off_diag"])

    def tri_diag_cond_func(state: TriDiagStateStruct) -> bool:
        # TODO: re-write this to use `jnp.logical_*`
        predicates = jnp.array([state.off_diag[state.t] > eps, state.t == 1], dtype=jnp.uint8)
        predicates = jnp.array([jnp.sum(predicates) > 0, state.t <= num_iters], dtype=jnp.uint8)
        return jnp.sum(predicates) == 2

    def tri_diag_body_func(state: TriDiagStateStruct) -> TriDiagStateStruct:
        V = state.V
        diag = state.diag
        off_diag = state.off_diag
        transformed_v = M(V[state.t]) - (off_diag[state.t] * V[state.t-1]) # heed the off_diag index
        diag = diag.at[state.t].set(jnp.dot(V[state.t], transformed_v))
        v_next = transformed_v - (diag[state.t] * V[state.t])  

        # full reorthogonalization here
        v_next = lax.fori_loop(
            1, state.t+1, lambda i, vec: vec - (jnp.dot(vec, V[i]) * V[i]), v_next)

        off_diag = off_diag.at[state.t+1].set(jnp.linalg.norm(v_next))
        v_next /= off_diag[state.t+1]
        V = V.at[state.t+1].set(v_next)
        return TriDiagStateStruct(
            t=state.t+1, V=V, diag=diag, off_diag=off_diag
        )

    v_1 = jax.random.normal(rng, shape=(n,))
    v_1 = v_1 / jnp.linalg.norm(v_1)

    # (*) dimension hacking to make it easier for jax
    V = jnp.zeros((num_iters+2, n)) 
    V = V.at[1].set(v_1)
    init_state = TriDiagStateStruct(
        t=1,
        V=V,
        diag=jnp.zeros((num_iters+1,)),
        off_diag=jnp.zeros((num_iters+2,)))

    final_state = lax.while_loop(tri_diag_cond_func, tri_diag_body_func, init_state)

    # remove dimension hacking initiated at (*)
    V = final_state.V[1:-1,:]
    diag = final_state.diag[1:]
    off_diag = final_state.off_diag[2:-1]

    min_k_eigvals = jax.scipy.linalg.eigh_tridiagonal(
        diag,
        off_diag,
        select="i",
        select_range=(0, k-1),
        eigvals_only=True)

    embed()
    exit()

    # Since jax only implements `eigh_tridiagonal` for eigenvalues, we need to compute
    # eigenvectors for ourselves. Below is adapted from tensorflow implementation:
    # https://github.com/tensorflow/tensorflow/blob/c1a369e066d94418ee4f6d8aeaf7fbe086441fc0/tensorflow/python/ops/linalg/linalg_impl.py#L1460-L1585
    @jax.named_scope("tridiag_eigvecs")
    def tridiag_eigvecs(diag, off_diag, eigvals):
        k = eigvals.size
        n = diag.size

        # Eigenvectors corresponding to cluster of close eigenvalues are
        # not unique and need to be explicitly orthogonalized. Here we
        # identify such clusters. Note: This function assumes that
        # eigenvalues are sorted in non-decreasing order.
        gap = eigvals[1:] - eigvals[:-1]
        t_norm = jnp.max(
            jnp.array([jnp.abs(eigvals[0]), jnp.abs(eigvals[-1])]))
        gaptol = jnp.sqrt(jnp.finfo(eigvals.dtype).eps) * t_norm

        # Find the beginning and end of runs of eigenvectors corresponding
        # to eigenvalues closer than "gaptol", which will need to be
        # orthogonalized against each other.
        close = jnp.less(gap, gaptol)
        cluster_mask = jnp.eye(k, dtype=bool) | jnp.diag(close, k=1) | jnp.diag(close, k=-1)
        cluster_mask = lax.fori_loop(0, k-2, lambda _, mask: mask @ mask, cluster_mask)

        # We perform inverse iteration for all eigenvectors in parallel,
        # starting from a random set of vectors, until all have converged.
        v0 = jax.random.normal(rng, shape=(k, n), dtype=off_diag.dtype)
        norm_v0 = jnp.linalg.norm(v0, axis=1)
        v0 = v0 / norm_v0.reshape(-1, 1)
        zero_norm = jnp.zeros(norm_v0.shape, dtype=norm_v0.dtype)

        # Replicate alpha-eigvals(ik) and beta across the k eigenvectors so we
        # can solve the k systems
        #    [T - eigvals(i)*eye(n)] x_i = r_i
        # simultaneously using the batching mechanism.
        eigvals_cast = eigvals.astype(dtype=off_diag.dtype)
        off_diag = jnp.tile(off_diag.reshape(1, -1), [k, 1])
        d = (diag.reshape(1, -1) - eigvals_cast.reshape(-1, 1))
        dl = jnp.concatenate([jnp.zeros((k, 1)), jnp.conj(off_diag)], axis=1)
        du = jnp.concatenate([off_diag, jnp.zeros((k, 1))], axis=1)

        def orthogonalize_close_eigenvectors(eigenvectors):
            # Eigenvectors corresponding to a cluster of close eigenvalues are not
            # uniquely defined, but the subspace they span is. To avoid numerical
            # instability, we explicitly mutually orthogonalize such eigenvectors
            # after each step of inverse iteration. It is customary to use
            # modified Gram-Schmidt for this, but this is not very efficient
            # on some platforms, so here we defer to the QR decomposition in JAX.

            def orthogonalize_cluster(i: int, eigenvectors: Array):
                # We use the builtin QR factorization to orthonormalize the
                # vectors in the cluster.
                cluster_mask_i = cluster_mask[i].reshape(-1, 1)
                q, _ = jnp.linalg.qr(jnp.transpose(cluster_mask_i * eigenvectors))
                update_vectors = jnp.transpose(q)
                eigenvectors = ((cluster_mask_i * update_vectors)
                                + (~cluster_mask_i * eigenvectors))
                return eigenvectors

            eigenvectors = lax.fori_loop(0, k, orthogonalize_cluster, eigenvectors)
            return eigenvectors

        def continue_iteration(state: Tuple[int, Array, Array, Array]):
            i, _, nrm_v, nrm_v_old = state
            max_it = 5  # Taken from LAPACK xSTEIN.
            min_norm_growth = 0.1
            norm_growth_factor = 1 + min_norm_growth
            # We stop the inverse iteration when we reach the maximum number of
            # iterations or the norm growths is less than 10%.
            return jnp.logical_and(
                jnp.less(i, max_it),
                jnp.any(
                    jnp.greater_equal(
                        jnp.real(nrm_v),
                        jnp.real(norm_growth_factor * nrm_v_old))))

        def inverse_iteration_step(state: Tuple[int, Array, Array, Array]):
            i, v, nrm_v, nrm_v_old = state
            v = lax.fori_loop(
                lower=0, 
                upper=k,
                body_fun=lambda i, v: v.at[i].set(
                    lax.linalg.tridiagonal_solve(
                        dl=dl[i], d=d[i], du=du[i], b=v[i].reshape(-1, 1)).reshape(-1,)),
                init_val=v)
            nrm_v_old = nrm_v
            nrm_v = jnp.linalg.norm(v, axis=1)
            v = v / nrm_v.reshape(-1, 1)
            # orthogonalize for numerical stability
            q, _ = jnp.linalg.qr(jnp.transpose(v))
            v = jnp.transpose(q)
            return i+1, v, nrm_v, nrm_v_old
        
        _, v, _, _ = lax.while_loop(
            continue_iteration, inverse_iteration_step, (0, v0, norm_v0, zero_norm))

        return jnp.transpose(v)

    min_k_eigvecs = tridiag_eigvecs(diag, off_diag, min_k_eigvals)

    # TODO: maybe assert that the eigvals are all negative (or at least some are)?

    jax.debug.print("\n Here! \n")
    embed()
    exit()


    V = np.empty((num_iters, n))
    omegas = np.empty((num_iters,))
    rhos = np.empty((num_iters - 1,))

    v_0 = np.random.normal(size=(n,))
    V[0] = v_0 / np.linalg.norm(v_0)

    for i in range(num_iters):
        transformed_v = M(V[i])
        omegas[i] = np.dot(V[i], transformed_v)
        if i == num_iters - 1:
            break  # we have all we need
        V[i + 1] = transformed_v - (omegas[i] * V[i])
        if i > 0:
            V[i + 1] -= rhos[i - 1] * V[i - 1]
        rhos[i] = np.linalg.norm(V[i + 1])
        if rhos[i] < eps:
            break
        V[i + 1] = V[i + 1] / rhos[i]

    min_eigen_val, u = eigh_tridiagonal(
        omegas[: i + 1], rhos[:i], select="i", select_range=(0, 0)
    )
    min_eigen_vec = (u.T @ V[: i + 1])
    # renormalize for stability
    min_eigen_vec = min_eigen_vec / np.linalg.norm(min_eigen_vec)

    #max_eigen_val, u = eigh_tridiagonal(
    #    omegas[: i + 1], rhos[:i], select="i", select_range=(i, i)
    #)
    #max_eigen_vec = (u.T @ V[: i + 1]).squeeze()
    ## renormalize for stability
    #max_eigen_vec = max_eigen_vec / np.linalg.norm(max_eigen_vec)

    return min_eigen_val.squeeze(), min_eigen_vec.T

# don't have to jit this function? just jaxpr since it's only called once? YES
def cgal(
    n: int,
    m: int,
    trace_ub: float,
    C_innerprod: Callable[[Array], float],
    C_add: Callable[[Array], Array],
    C_matvec: Callable[[Array], Array],
    A_operator: Callable[[Array], Array],
    A_operator_slim: Callable[[Array], Array],
    A_adjoint: Callable[[Array], Array],
    A_adjoint_slim: Callable[[Array, Array], Array],
    proj_K: Callable[[Array], Array],
    beta: float,
    SCALE_C: float,
    SCALE_X: float,
    eps: float,
    max_iters: int
) -> Tuple[Array, Array]:

    StateStruct = namedtuple(
        "StateStruct", 
        ["t", "X", "y", "obj_gap", "infeas_gap"])

    @jax.jit
    def cond_func(state: StateStruct) -> bool:
        # hacky jax-compatible implementation of the following predicate (to continue optimizing):
        #   (obj_gap > eps or infeas_gap > eps) and state.t < max_iters
        # TODO: re-write this to use `jnp.logical_*`
        predicates = jnp.array([state.obj_gap > eps, state.infeas_gap > eps], dtype=jnp.uint8)
        predicates = jnp.array([jnp.sum(predicates) > 0, state.t < max_iters], dtype=jnp.uint8)
        return jnp.sum(predicates) == 2

    @jax.jit
    def body_func(state: StateStruct) -> StateStruct:
        z = A_operator(state.X)
        b = proj_K(z + (state.y / beta))
        grad = C_add(A_adjoint(state.y + beta*(z - b)))
        eigvals, eigvecs = jnp.linalg.eigh(grad)
        # TODO: report eigval gap here!
        min_eigval = eigvals[0]
        min_eigvec = eigvecs[:, 0:1]  # gives the right shape
        X_update_dir = min_eigvec @ min_eigvec.T
        eta = 2.0 / (state.t + 2.0)   # just use the standard CGAL step-size for now
        surrogate_dual_gap = jnp.trace(grad @ (state.X - X_update_dir))
        obj_gap = surrogate_dual_gap - jnp.dot(state.y, z - b) - 0.5*beta*jnp.linalg.norm(z - b)**2
        obj_gap = obj_gap / (SCALE_C * SCALE_X)
        infeas_gap = jnp.max(jnp.abs(z - proj_K(z))) / SCALE_X
        jax.debug.print("t: {t} - obj_val: {obj_val} - obj_gap: {obj_gap} - infeas_gap: {infeas_gap}",
                        t=state.t,
                        obj_val=C_innerprod(state.X) / (SCALE_C * SCALE_X),
                        obj_gap=obj_gap,
                        infeas_gap=infeas_gap)
        X_next = (1-eta)*state.X + eta*X_update_dir
        z_next = A_operator(X_next)
        y_next = state.y + (z_next - proj_K(z_next + (state.y / beta)))
        return StateStruct(
            t=state.t+1,
            X=X_next,
            y=y_next,
            obj_gap=obj_gap,
            infeas_gap=infeas_gap)



    init_state = StateStruct(
        t=0,
        X=jnp.zeros((n, n)) * SCALE_X,
        y=jnp.zeros((m,)),
        obj_gap=1.1*eps,
        infeas_gap=1.1*eps)

    final_state = lax.while_loop(cond_func, body_func, init_state)

    embed()
    exit()

    return final_state.X, final_state.y