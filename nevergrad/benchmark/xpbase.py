# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import sys
import time
import random
import warnings
import traceback
from typing import Dict, Union, Any, Optional, Iterator, Type, Callable
import numpy as np
from nevergrad.parametrization import parameter as p
from ..common import decorators
from ..functions.rl.agents import torch  # import includes pytorch fix
from ..functions import base as fbase
from ..optimization import base as obase
from ..optimization.optimizerlib import registry as optimizer_registry  # import from optimizerlib so as to fill it
from . import execution

registry = decorators.Registry[Callable[..., Iterator['Experiment']]]()


class OptimizerSettings:
    """Handle for optimizer settings (name, num_workers etc)
    Optimizers can be instantiated through this class, providing the optimization space dimension.

    Note
    ----
    Eventually, this class should be moved to be directly used for defining experiments.
    """

    def __init__(self, optimizer: Union[str, obase.OptimizerFamily], budget: int, num_workers: int = 1, batch_mode: bool = True) -> None:
        self._setting_names = [x for x in locals() if x != "self"]
        if isinstance(optimizer, str):
            assert optimizer in optimizer_registry, f"{optimizer} is not registered"
        self.optimizer = optimizer
        self.budget = budget
        self.num_workers = num_workers
        self.executor = execution.MockedTimedExecutor(batch_mode)

    @property
    def name(self) -> str:
        return self.optimizer if isinstance(self.optimizer, str) else repr(self.optimizer)

    @property
    def batch_mode(self) -> bool:
        return self.executor.batch_mode

    def __repr__(self) -> str:
        return f"Experiment: {self.name}<budget={self.budget}, num_workers={self.num_workers}, batch_mode={self.batch_mode}>"

    def _get_factory(self) -> Union[Type[obase.Optimizer], obase.OptimizerFamily]:
        return optimizer_registry[self.optimizer] if isinstance(self.optimizer, str) else self.optimizer

    @property
    def is_incoherent(self) -> bool:
        """Flags settings which are known to be impossible to process.
        Currently, this means we flag:
        - no_parallelization optimizers for num_workers > 1
        """
        # flag no_parallelization when num_workers greater than 1
        return self._get_factory().no_parallelization and bool(self.num_workers > 1)

    def instantiate(self, instrumentation: p.Parameter) -> obase.Optimizer:
        """Instantiate an optimizer, providing the optimization space dimension
        """
        return self._get_factory()(instrumentation=instrumentation, budget=self.budget, num_workers=self.num_workers)

    def get_description(self) -> Dict[str, Any]:
        """Returns a dictionary describing the optimizer settings
        """
        descr = {x: getattr(self, x) for x in self._setting_names if x != "optimizer"}
        descr["optimizer_name"] = self.name
        return descr

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, self.__class__):
            return all(getattr(self, attr) == getattr(other, attr) for attr in self._setting_names)
        return False


def create_seed_generator(seed: Optional[int]) -> Iterator[Optional[int]]:
    """Create a stream of seeds, independent from the standard random stream.
    This is designed to be used in experiment plans generators, fore reproducibility.

    Parameter
    ---------
    seed: int or None
        the initial seed

    Yields
    ------
    int or None
        potential new seeds, or None if the initial seed was None
    """
    generator = None if seed is None else np.random.RandomState(seed=seed)
    while True:
        yield None if generator is None else generator.randint(2**32, dtype=np.uint32)


class Experiment:
    """Specifies an experiment which can be run in benchmarks.

    Parameters
    ----------
    function: ExperimentFunction
        the function to run the experiment on. It must inherit from ExperimentFunction to implement
        necessary functionalities (parametrization, descriptors, evaluation_function, pseudotime etc)

    Note
    ----
    - "run" method catches error but forwards stderr so that errors are not completely hidden
    - "run" method outputs the description of the experiment, which is a set of figures/names from the functions
    settings (dimension, etc...), the optimization settings (budget, etc...) and the results (loss, etc...)
    """

    # pylint: disable=too-many-arguments
    def __init__(self, function: fbase.ExperimentFunction,
                 optimizer: Union[str, obase.OptimizerFamily], budget: int, num_workers: int = 1,
                 batch_mode: bool = True, seed: Optional[int] = None,
                 ) -> None:
        assert isinstance(function, fbase.ExperimentFunction), ("All experiment functions should "
                                                                "derive from ng.functions.ExperimentFunction")
        assert function.dimension, "Nothing to optimize"
        self.function = function
        self.seed = seed  # depending on the inner workings of the function, the experiment may not be repeatable
        self.optimsettings = OptimizerSettings(optimizer=optimizer, num_workers=num_workers, budget=budget, batch_mode=batch_mode)
        self.result = {"loss": np.nan, "elapsed_budget": np.nan, "elapsed_time": np.nan, "error": ""}
        self.recommendation: Optional[p.Parameter] = None
        self._optimizer: Optional[obase.Optimizer] = None  # to be able to restore stopped/checkpointed optimizer

    def __repr__(self) -> str:
        return f"Experiment: {self.optimsettings} (dim={self.function.dimension}) on {self.function}"

    @property
    def is_incoherent(self) -> bool:
        """Flags settings which are known to be impossible to process.
        Currently, this means we flag:
        - no_parallelization optimizers for num_workers > 1
        """
        return self.optimsettings.is_incoherent

    def run(self) -> Dict[str, Any]:
        """Run an experiment with the provided settings

        Returns
        -------
        dict
            A dict containing all the information about the experiments (optimizer/function settings + results)

        Note
        ----
        This function catches error (but forwards stderr). It fills up the "error" ("" if no error, else the error name),
        "loss", "elapsed_time" and "elapsed_budget" of the experiment.
        """
        try:
            self._run_with_error()
        except fbase.ExperimentFunctionCopyError as c_e:
            raise c_e
        except Exception as e:  # pylint: disable=broad-except
            # print the case and the traceback
            self.result["error"] = e.__class__.__name__
            print(f"Error when applying {self}:", file=sys.stderr)
            traceback.print_exc()
            print("\n", file=sys.stderr)
        return self.get_description()

    def _log_results(self, pfunc: fbase.ExperimentFunction, t0: float, num_calls: int) -> None:
        """Internal method for logging results before handling the error
        """
        self.result["elapsed_time"] = time.time() - t0
        self.result["pseudotime"] = self.optimsettings.executor.time
        # make a final evaluation with oracle (no noise, but function may still be stochastic)
        assert self.recommendation is not None
        reco = self.recommendation
        self.result["loss"] = pfunc.evaluation_function(*reco.args, **reco.kwargs)  # ExperimentFunction directly override this if need be
        self.result["elapsed_budget"] = num_calls
        if num_calls > self.optimsettings.budget:
            raise RuntimeError(f"Too much elapsed budget {num_calls} for {self.optimsettings.name} on {self.function}")

    def _run_with_error(self, callbacks: Optional[Dict[str, obase._OptimCallBack]] = None) -> None:
        """Run an experiment with the provided artificial function and optimizer

        Parameter
        ---------
        callbacks: dict
            a dictionary of callbacks to register on the optimizer with key "ask" and/or "tell" (see base Optimizer class).
            This is only for easier debugging.
        """
        if self.seed is not None and self._optimizer is None:
            # Note: when resuming a job (if optimizer is not None), seeding is pointless (reproducibility is lost)
            np.random.seed(self.seed)  # seeds both functions and instrumentation (for which random state init is lazy)
            random.seed(self.seed)
            torch.manual_seed(self.seed)  # type: ignore
        pfunc = self.function.copy()
        # check constraints are propagated
        assert len(pfunc.parametrization._constraint_checkers) == len(self.function.parametrization._constraint_checkers)
        # optimizer instantiation can be slow and is done only here to make xp iterators very fast
        if self._optimizer is None:
            self._optimizer = self.optimsettings.instantiate(instrumentation=pfunc.parametrization)
        if callbacks is not None:
            for name, func in callbacks.items():
                self._optimizer.register_callback(name, func)
        assert self._optimizer.budget is not None, "A budget must be provided"
        t0 = time.time()
        executor = self.optimsettings.executor
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=obase.InefficientSettingsWarning)  # benchmark do not need to be efficient
            try:
                # call the actual Optimizer.minimize method because overloaded versions could alter the worklflow
                # and provide unfair comparisons  (especially for parallelized settings)
                self.recommendation = obase.Optimizer.minimize(
                    self._optimizer,
                    pfunc,
                    batch_mode=executor.batch_mode,
                    executor=executor
                )
            except Exception as e:  # pylint: disable=broad-except
                self.recommendation = self._optimizer.provide_recommendation()  # get the recommendation anyway
                self._log_results(pfunc, t0, self._optimizer.num_ask)
                raise e
        self._log_results(pfunc, t0, self._optimizer.num_ask)

    def get_description(self) -> Dict[str, Union[str, float, bool]]:
        """Return the description of the experiment, as a dict.
        "run" must be called beforehand in order to have non-nan values for the loss.
        """
        summary = dict(self.result, seed=-1 if self.seed is None else self.seed)
        summary.update(self.function.descriptors)
        summary.update(self.optimsettings.get_description())
        return summary

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Experiment):
            return False
        same_seed = other.seed is None if self.seed is None else other.seed == self.seed
        return same_seed and self.function.equivalent_to(other.function) and self.optimsettings == other.optimsettings
