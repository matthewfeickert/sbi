# This file is part of sbi, a toolkit for simulation-based inference. sbi is licensed
# under the Affero General Public License v3, see <https://www.gnu.org/licenses/>.
from functools import partial
from math import ceil
from typing import Any, Callable, Dict, List, Optional, Union
from warnings import warn

import numpy as np
import torch
import torch.distributions.transforms as torch_tf
from pyro.infer.mcmc import HMC, NUTS
from pyro.infer.mcmc.api import MCMC
from torch import Tensor
from torch import multiprocessing as mp
from torch import nn

from sbi import utils as utils
from sbi.analysis import gradient_ascent
from sbi.inference.posteriors.base_posterior import NeuralPosterior
from sbi.samplers.mcmc import (
    IterateParameters,
    Slice,
    SliceSampler,
    SliceSamplerVectorized,
    prior_init,
    sir,
)
from sbi.types import Shape, TorchTransform
from sbi.utils import (
    del_entries,
    mcmc_transform,
    pyro_potential_wrapper,
    transformed_potential,
)
from sbi.utils.torchutils import (
    atleast_2d,
    atleast_2d_float32_tensor,
    ensure_theta_batched,
)


class MCMCPosterior(NeuralPosterior):
    r"""Provides MCMC to sample from the posterior.<br/><br/>
    SNLE or SNRE train neural networks to approximate the likelihood(-ratios).
    `MCMCPosterior` allows to sample from the posterior with MCMC.
    """

    def __init__(
        self,
        potential_fn: Callable,
        prior: Any,
        theta_transform: Optional[TorchTransform] = None,
        method: str = "slice_np",
        thin: int = 10,
        warmup_steps: int = 10,
        num_chains: int = 1,
        init_strategy: str = "prior",
        init_strategy_num_candidates: int = 1_000,
        device: Optional[str] = None,
        x_shape: Optional[torch.Size] = None,
    ):
        """
        Args:
            potential_fn: The potential function from which to draw samples.
            prior: Prior distribution. Is used to initialize the chain.
            theta_transform: Transformation that will be applied during sampling.
                Allows to perform MCMC in unconstrained space.
            method: Method used for MCMC sampling, one of `slice_np`, `slice`,
                `hmc`, `nuts`. Currently defaults to `slice_np` for a custom numpy
                implementation of slice sampling; select `hmc`, `nuts` or `slice` for
                Pyro-based sampling.
            thin: set the thinning factor for the chain
            warmup_steps: set the initial number of
                samples to discard
            num_chains: for the number of chains,
            init_strategy: the initialisation strategy for chains; `prior`
                will draw init locations from prior, whereas `sir` will use Sequential-
                Importance-Resampling
            init_strategy_num_candidates: Number of candidates to to find init
                locations in `init_strategy=sir`.
            device: Training device, e.g., "cpu", "cuda" or "cuda:0". If None,
                `potential_fn.device` is used.
            x_shape: Shape of a single simulator output. If passed, it is used to check
                the shape of the observed data and give a descriptive error.
        """

        super().__init__(
            potential_fn,
            theta_transform=theta_transform,
            device=device,
            x_shape=x_shape,
        )

        self.prior = prior
        self.method = method
        self.thin = thin
        self.warmup_steps = warmup_steps
        self.num_chains = num_chains
        self.init_strategy = init_strategy
        self.init_strategy_num_candidates = init_strategy_num_candidates

        if method == "slice":
            track_gradients = False
            pyro = True
        elif method in ("hmc", "nuts"):
            track_gradients = True
            pyro = True
        elif "slice_np" in method:
            track_gradients = False
            pyro = False
        else:
            raise NotImplementedError

        self.potential_ = partial(
            transformed_potential,
            potential_fn=potential_fn,
            theta_transform=self.theta_transform,
            device=self._device,
            track_gradients=track_gradients,
        )
        if pyro:
            self.potential_ = partial(pyro_potential_wrapper, potential=self.potential_)

        self._purpose = (
            "It provides MCMC to .sample() from the posterior and "
            "can evaluate the _unnormalized_ posterior density with .log_prob()."
        )

    @property
    def mcmc_method(self) -> str:
        """Returns MCMC method."""
        return self._mcmc_method

    @mcmc_method.setter
    def mcmc_method(self, method: str) -> None:
        """See `set_mcmc_method`."""
        self.set_mcmc_method(method)

    def set_mcmc_method(self, method: str) -> "NeuralPosterior":
        """Sets sampling method to for MCMC and returns `NeuralPosterior`.

        Args:
            method: Method to use.

        Returns:
            `NeuralPosterior` for chainable calls.
        """
        self._mcmc_method = method
        return self

    def sample(
        self,
        sample_shape: Shape = torch.Size(),
        x: Optional[Tensor] = None,
        show_progress_bars: bool = True,
    ) -> Tensor:
        r"""
        Return samples from posterior distribution $p(\theta|x)$ with MCMC.

        Args:
            sample_shape: Desired shape of samples that are drawn from posterior. If
                sample_shape is multidimensional we simply draw `sample_shape.numel()`
                samples and then reshape into the desired shape.
            show_progress_bars: Whether to show sampling progress monitor.

        Returns:
            Samples from posterior.
        """
        self.potential_fn.set_x(self._x_else_default_x(x))

        init_fn = self._build_mcmc_init_fn(
            self.prior, self.potential_fn, transform=self.theta_transform
        )
        initial_params = torch.cat([init_fn() for _ in range(self.num_chains)])

        num_samples = torch.Size(sample_shape).numel()

        track_gradients = self.method in ("hmc", "nuts")
        with torch.set_grad_enabled(track_gradients):
            if self.method in ("slice_np", "slice_np_vectorized"):
                transformed_samples = self._slice_np_mcmc(
                    num_samples=num_samples,
                    potential_function=self.potential_,
                    initial_params=initial_params,
                    thin=self.thin,
                    warmup_steps=self.warmup_steps,
                    vectorized=(self.method == "slice_np_vectorized"),
                    show_progress_bars=show_progress_bars,
                )
            elif self.method in ("hmc", "nuts", "slice"):
                transformed_samples = self._pyro_mcmc(
                    num_samples=num_samples,
                    potential_function=self.potential_,
                    initial_params=initial_params,
                    mcmc_method=self.method,
                    thin=self.thin,
                    warmup_steps=self.warmup_steps,
                    num_chains=self.num_chains,
                    show_progress_bars=show_progress_bars,
                ).detach()
            else:
                raise NameError

        samples = self.theta_transform.inv(transformed_samples)
        return samples.reshape((*sample_shape, -1))

    def _build_mcmc_init_fn(
        self,
        prior: Any,
        potential_fn: Callable,
        transform: torch_tf.Transform,
        init_strategy: str = "prior",
        **kwargs,
    ) -> Callable:
        """
        Return function that, when called, creates an initial parameter set for MCMC.

        Args:
            prior: Prior distribution.
            potential_fn: Potential function that the candidate samples are weighted
                with.
            init_strategy: Specifies the initialization method. Either of
                [`prior`|`sir`|`latest_sample`].
            kwargs: Passed on to init function. This way, init specific keywords can
                be set through `mcmc_parameters`. Unused arguments should be absorbed.

        Returns: Initialization function.
        """
        if init_strategy == "prior":
            return lambda: prior_init(prior, transform=transform, **kwargs)
        elif init_strategy == "sir":
            return lambda: sir(prior, potential_fn, transform=transform, **kwargs)
        elif init_strategy == "latest_sample":
            latest_sample = IterateParameters(self._mcmc_init_params, **kwargs)
            return latest_sample
        else:
            raise NotImplementedError

    def _slice_np_mcmc(
        self,
        num_samples: int,
        potential_function: Callable,
        initial_params: Tensor,
        thin: int,
        warmup_steps: int,
        vectorized: bool = False,
        show_progress_bars: bool = True,
    ) -> Tensor:
        """
        Custom implementation of slice sampling using Numpy.

        Args:
            num_samples: Desired number of samples.
            potential_function: A callable **class**.
            initial_params: Initial parameters for MCMC chain.
            thin: Thinning (subsampling) factor.
            warmup_steps: Initial number of samples to discard.
            vectorized: Whether to use a vectorized implementation of
                the Slice sampler (still experimental).
            show_progress_bars: Whether to show a progressbar during sampling;
                can only be turned off for vectorized sampler.

        Returns: Tensor of shape (num_samples, shape_of_single_theta).
        """
        num_chains = initial_params.shape[0]
        dim_samples = initial_params.shape[1]

        if not vectorized:  # Sample all chains sequentially
            all_samples = []
            for c in range(num_chains):
                posterior_sampler = SliceSampler(
                    utils.tensor2numpy(initial_params[c, :]).reshape(-1),
                    lp_f=potential_function,
                    thin=thin,
                    verbose=show_progress_bars,
                )
                if warmup_steps > 0:
                    posterior_sampler.gen(int(warmup_steps))
                all_samples.append(
                    posterior_sampler.gen(ceil(num_samples / num_chains))
                )
            all_samples = np.stack(all_samples).astype(np.float32)
            samples = torch.from_numpy(all_samples)  # chains x samples x dim
        else:  # Sample all chains at the same time
            posterior_sampler = SliceSamplerVectorized(
                init_params=utils.tensor2numpy(initial_params),
                log_prob_fn=potential_function,
                num_chains=num_chains,
                verbose=show_progress_bars,
            )
            warmup_ = warmup_steps * thin
            num_samples_ = ceil((num_samples * thin) / num_chains)
            samples = posterior_sampler.run(warmup_ + num_samples_)
            samples = samples[:, warmup_:, :]  # discard warmup steps
            samples = samples[:, ::thin, :]  # thin chains
            samples = torch.from_numpy(samples)  # chains x samples x dim

        # Save sample as potential next init (if init_strategy == 'latest_sample').
        self._mcmc_init_params = samples[:, -1, :].reshape(num_chains, dim_samples)

        samples = samples.reshape(-1, dim_samples)[:num_samples, :]
        assert samples.shape[0] == num_samples

        return samples.type(torch.float32).to(self._device)

    def _pyro_mcmc(
        self,
        num_samples: int,
        potential_function: Callable,
        initial_params: Tensor,
        mcmc_method: str = "slice",
        thin: int = 10,
        warmup_steps: int = 200,
        num_chains: Optional[int] = 1,
        show_progress_bars: bool = True,
    ):
        r"""Return samples obtained using Pyro HMC, NUTS for slice kernels.

        Args:
            num_samples: Desired number of samples.
            potential_function: A callable **class**. A class, but not a function,
                is picklable for Pyro MCMC to use it across chains in parallel,
                even when the potential function requires evaluating a neural network.
            mcmc_method: One of `hmc`, `nuts` or `slice`.
            thin: Thinning (subsampling) factor.
            warmup_steps: Initial number of samples to discard.
            num_chains: Whether to sample in parallel. If None, use all but one CPU.
            show_progress_bars: Whether to show a progressbar during sampling.

        Returns: Tensor of shape (num_samples, shape_of_single_theta).
        """
        num_chains = mp.cpu_count - 1 if num_chains is None else num_chains

        kernels = dict(slice=Slice, hmc=HMC, nuts=NUTS)

        sampler = MCMC(
            kernel=kernels[mcmc_method](potential_fn=potential_function),
            num_samples=(thin * num_samples) // num_chains + num_chains,
            warmup_steps=warmup_steps,
            initial_params={"": initial_params},
            num_chains=num_chains,
            mp_context="fork",
            disable_progbar=not show_progress_bars,
            transforms={},
        )
        sampler.run()
        samples = next(iter(sampler.get_samples().values())).reshape(
            -1, initial_params.shape[1]  # .shape[1] = dim of theta
        )

        samples = samples[::thin][:num_samples]
        assert samples.shape[0] == num_samples

        return samples

    def map(
        self,
        x: Optional[Tensor] = None,
        num_iter: int = 1_000,
        num_to_optimize: int = 100,
        learning_rate: float = 0.01,
        init_method: Union[str, Tensor] = "prior",
        num_init_samples: int = 1_000,
        save_best_every: int = 10,
        show_progress_bars: bool = False,
    ) -> Tensor:
        r"""
        Returns the maximum-a-posteriori estimate (MAP).

        The method can be interrupted (Ctrl-C) when the user sees that the
        log-probability converges. The best estimate will be saved in `self.map_`.
        The MAP is obtained by running gradient ascent from a given number of starting
        positions (samples from the posterior with the highest log-probability). After
        the optimization is done, we select the parameter set that has the highest
        log-probability after the optimization.

        Warning: The default values used by this function are not well-tested. They
        might require hand-tuning for the problem at hand.

        For developers: if the prior is a `BoxUniform`, we carry out the optimization
        in unbounded space and transform the result back into bounded space.

        Args:
            num_iter: Number of optimization steps that the algorithm takes
                to find the MAP.
            learning_rate: Learning rate of the optimizer.
            init_method: How to select the starting parameters for the optimization. If
                it is a string, it can be either [`posterior`, `prior`], which samples
                the respective distribution `num_init_samples` times. If it is a
                tensor, the tensor will be used as init locations.
            num_init_samples: Draw this number of samples from the posterior and
                evaluate the log-probability of all of them.
            num_to_optimize: From the drawn `num_init_samples`, use the
                `num_to_optimize` with highest log-probability as the initial points
                for the optimization.
            save_best_every: The best log-probability is computed, saved in the
                `map`-attribute, and printed every `save_best_every`-th iteration.
                Computing the best log-probability creates a significant overhead
                (thus, the default is `10`.)
            show_progress_bars: Whether or not to show a progressbar for sampling from
                the posterior.
            log_prob_kwargs: Will be empty for SNLE and SNRE. Will contain
                {'norm_posterior': True} for SNPE.

        Returns:
            The MAP estimate.
        """
        self.potential_fn.set_x(self._x_else_default_x(x))

        if init_method == "posterior":
            inits = self.sample((num_init_samples,))
        elif init_method == "prior":
            inits = self.prior.sample((num_init_samples,))
        else:
            raise ValueError

        self.map_ = gradient_ascent(
            potential_fn=self.potential_fn,
            inits=inits,
            theta_transform=self.theta_transform,
            num_iter=num_iter,
            num_to_optimize=num_to_optimize,
            learning_rate=learning_rate,
            save_best_every=save_best_every,
            show_progress_bars=show_progress_bars,
        )[0]
        return self.map_