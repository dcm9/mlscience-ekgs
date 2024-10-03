from functools import partial
from typing import Sequence
from tqdm import tqdm

import flax.linen as nn
from flax.training import train_state
import jax
from jax.flatten_util import ravel_pytree
from jax.tree_util import tree_map
import jax.numpy as jnp
import jax.random as jr
import optax
import tensorflow_probability.substrates.jax as tfp


tfd = tfp.distributions
MVN = tfd.MultivariateNormalTriL
MVD = tfd.MultivariateNormalDiag
Normal = tfd.Normal


OE = jnp.array([
    [1., -1., 0.],
    [0., -1., 1.],
    [-1., 0., 1.]
])
OA = jnp.array([
    [-0.5, 1., -0.5],
    [1., -0.5, -0.5],
    [-0.5, -0.5, 1.]
])
OV = -1/3 * jnp.ones((6, 3))
OH = jnp.vstack((jnp.zeros((6, 6)), jnp.eye(6)))
OMAT = jnp.hstack((jnp.vstack((OE, OA, OV)), OH))

R_PRIOR = jnp.array([
    [0.125, 0., 0.125],
    [-0.125, 0., 0.125],
    [0.125, 0., -0.125],
    [0.125*jnp.cos(100/180*jnp.pi), -0.125/2.75*jnp.sin(100/180*jnp.pi), 0.],
    [0.125*jnp.cos(80/180*jnp.pi), -0.125/2.75*jnp.sin(80/180*jnp.pi), 0.],
    [0.125*jnp.cos(60/180*jnp.pi), -0.125/2.75*jnp.sin(60/180*jnp.pi), 0.],
    [0.125*jnp.cos(40/180*jnp.pi), -0.125/2.75*jnp.sin(40/180*jnp.pi), 0.],
    [0.125*jnp.cos(20/180*jnp.pi), -0.125/2.75*jnp.sin(20/180*jnp.pi), 0.],
    [0.125*jnp.cos(0/180*jnp.pi), -0.125/2.75*jnp.sin(0/180*jnp.pi), 0.],
])

# def f1(x):
#     return x[0]

# def f2(x):
#     return x[0] + x[1]

# def generate_anomalies(f1, f2, x0, n_iter=1000, tol=1.0, lr=1e-2):
#     x_morphed = x0.copy()
#     for _ in range(n_iter):
#         x_delta = grad(f2)(x_morphed)
#         grad1 = grad(f1)(x_morphed)
#         # orthogonalize with respect to f1
#         x_delta = x_delta - (x_delta @ grad1) * grad1/jnp.linalg.norm(grad1)**2
#         x_morphed = x_morphed + lr * x_delta

#         if jnp.abs(f2(x_morphed) - f2(x0)) > tol:
#             break
#     if jnp.abs(f2(x_morphed) - f2(x0)) > tol:
#         print(f"x_original: {x0}")
#         print(f"x_morphed : {x_morphed}\n")
#         print(f"f1(x_original): {f1(x0)}")
#         print(f"f2(x_original): {f2(x0)}\n")
#         print(f"f1(x_morphed) : {f1(x_morphed)}")
#         print(f"f2(x_morphed) : {f2(x_morphed)}")

#         return x0, x_morphed

#     return None, None
    
def gaussian_kl(mu, sigmasq):
    """KL divergence from a diagonal Gaussian to the standard Gaussian."""
    return -0.5 * jnp.sum(1. + jnp.log(sigmasq) - mu**2. - sigmasq)

def gaussian_sample(key, mu, sigmasq):
    """Sample a diagonal Gaussian."""
    return mu + jnp.sqrt(sigmasq) * jr.normal(key, mu.shape)

def gaussian_logpdf(x_pred, x):
    """Gaussian log pdf of data x given x_pred."""
    return -0.5 * jnp.sum((x - x_pred)**2., axis=-1)

def get_sigmas(sigma_min=0.01, sigma_max=1.0, num_scales=10):
    sigmas = jnp.exp(
        jnp.linspace(
            jnp.log(sigma_max), jnp.log(sigma_min), num_scales
        )
    )

    return sigmas

def compute_electrode_electric_potential(s, p, r, kappa=0.2):
    """Compute the electric potential due to a single dipole.
    
    Args:
        s: 3-d location of the dipole
        p: 3-d moment of the dipole
        r: 3-d location of the electrode
        kappa: electrical conductivity of the torso
        
    Returns:
        e_p: electric potential at the electrode, in millivolts
    """
    d = r - s # 3-d displacement vector
    e_p = 1/(4*jnp.pi*kappa) * (d @ p)/jnp.linalg.norm(d)**3
    e_p *= 1e3 # Convert to millivolts

    return e_p

def compute_log_likelihood(params, obs, obs_mask, obs_scale=0.1):
    """Compute the log likelihood of the model.
    
    Args:
        params: parameters of the model.
        obs: observed lead observations.
        obs_mask: mask for the observed lead observations.
        obs_scale: scale of the observation noise.
        
    Returns:
        log_likelihood: log likelihood of the model.
    """
    r, s, p = params["r"], params["s"], params["p"]
    eps = jax.vmap(
        jax.vmap(
            compute_electrode_electric_potential, (0, 0, None)
        ), (None, None, 0)
    )(s, p, r)
    leads_pred = OMAT @ eps
    ll_fn = lambda pred, obs: Normal(loc=pred, scale=obs_scale).log_prob(obs)
    log_likelihood = jax.vmap(ll_fn)(leads_pred, obs)
    log_likelihood = jnp.sum(log_likelihood * obs_mask)
    
    return log_likelihood

def compute_log_joint_prob(params, obs, obs_mask, 
                           r_prior, s_prior, p_prior, obs_scale=0.01):
    """Compute the log joint probability of the model.

    Args:
        params: parameters of the model.
        obs: observed lead observations.
        obs_mask: mask for the observed lead observations.
        r_prior: prior distribution for the location of the electrode.
        s_prior: prior distribution for the location of the dipole.
        p_prior: prior distribution for the moment of the dipole.
        obs_scale: scale of the observation noise.

    Returns:
        log_joint: log joint probability of the model.
    """
    r, s, p = params["r"], params["s"], params["p"]
    log_likelihood = compute_log_likelihood(params, obs, obs_mask, obs_scale)
    # Assume Gaussian prior
    log_prior_r = r_prior.log_prob(r).sum()
    log_prior_s = s_prior.log_prob(s).sum()
    log_prior_p = p_prior.log_prob(p).sum()
    log_prior = log_prior_r + log_prior_s + log_prior_p
    log_joint = log_likelihood + log_prior
    
    return log_joint

def ndipole_compute_electrode_electric_potential(s, p, r, kappa=0.2):
    """Compute the electric potential due to n dipoles.
    
    Args:
        s (n, 3): locations of the n dipoles
        p (n, 3): moments of the n dipole
        r: 3-d location of the electrode
        kappa: electrical conductivity of the torso
        
    Returns:
        e_p: electric potential at the electrode
    """
    eps = jax.vmap(
        compute_electrode_electric_potential, (0, 0, None, None)
    )(s, p, r, kappa)
    e_p = jnp.sum(eps, axis=0)

    return e_p

def compute_linproj_residual(x):
    """Compute the residual of the linear projection.

    Args:
        x (jnp.array): 12d lead observation.

    Returns:
        res (jnp.array): residual of the linear projection.
    """    
    sol, res, *_ = jnp.linalg.lstsq(OMAT, x, rcond=None)
    
    return sol, res