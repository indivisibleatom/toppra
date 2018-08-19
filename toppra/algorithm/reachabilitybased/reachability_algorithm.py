from ..algorithm import ParameterizationAlgorithm
from ...solverwrapper import cvxpyWrapper, qpOASESSolverWrapper, ecosWrapper, hotqpOASESSolverWrapper
from ...constants import LARGE, SMALL
from ...constraint import ConstraintType

import numpy as np
import logging
logger = logging.getLogger(__name__)


class ReachabilityAlgorithm(ParameterizationAlgorithm):
    """Base class for all Reachability Analysis-based parameterization algorithms.


    Parameters
    ----------
    constraint_list: list of Constraint
    path: Interpolator
    gridpoints: (N+1,)array, optional
    solver_wrapper: str, optional
        Name of solver to use. If leave to be None, will select the
        most suitable solver wrapper.

    Notes
    -----
    There are two patterns this class implements.

    1) Together with `SolverWrapper`, it uses a Strategy pattern to achieve different
       solver configuration.

    2) The class itself implements a `Template` pattern.

    RA-based algorithm uses a `SolverWrapper` for most, if not all, computations.
    During initialization, a solver wrapper is constructed from the given variables
    and is used afterward.

    In addition to a generic path parameterization algorithm, a RA-based algorithm
    implement additionally three methods:
    - compute_controllable_sets
    - compute_reachable_sets
    - compute_feasible_sets
    """
    def __init__(self, constraint_list, path, gridpoints=None, solver_wrapper=None):
        super(ReachabilityAlgorithm, self).__init__(constraint_list, path, gridpoints=gridpoints)

        logger.debug("Checking supplied constraints.")
        has_conic = False
        for c in constraint_list:
            if c.get_constraint_type() == ConstraintType.CanonicalConic:
                has_conic = True
    
        if solver_wrapper is None:
            logger.debug("Solver wrapper not supplied. Choose solver wrapper automatically!")
            if has_conic:
                solver_wrapper = "ecos"
            else:
                solver_wrapper = "qpOASES"
            logger.debug("Select solver {:}".format(solver_wrapper))
        else:
            if has_conic:
                assert solver_wrapper.lower() in ['cvxpy', 'ecos'], "Problem has conic constraints, solver {:} is not suitable".format(solver_wrapper)
            else:
                assert solver_wrapper.lower() in ['cvxpy', 'qpoases', 'ecos', 'hotqpoases'], "Solver {:} not found".format(solver_wrapper)

        # Select
        if solver_wrapper.lower() == "cvxpy":
            self.solver_wrapper = cvxpyWrapper(self.constraints, self.path, self.gridpoints)
        elif solver_wrapper.lower() == "qpoases":
            self.solver_wrapper = qpOASESSolverWrapper(self.constraints, self.path, self.gridpoints)
        elif solver_wrapper.lower() == "hotqpoases":
            self.solver_wrapper = hotqpOASESSolverWrapper(self.constraints, self.path, self.gridpoints)
        elif solver_wrapper.lower() == "ecos":
            self.solver_wrapper = ecosWrapper(self.constraints, self.path, self.gridpoints)
        else:
            raise NotImplementedError("Solver wrapper {:} not found!".format(solver_wrapper))

    def compute_feasible_sets(self):
        """Compute the sets of feasible squared velocities.

        Returns
        -------
        X: (N+1,2)array,
            X[i] contains the lower and upper bound of the feasible
            squared path velocity at s[i].  If there is no feasible
            state, X[i] equals (np.nan, np.nan).

        """
        logger.info("Start computing the feasible sets")
        nV = self.solver_wrapper.get_no_vars()
        Hzero = np.zeros((nV, nV))
        g_lower = np.zeros(nV)
        g_lower[0] = 1e-9
        g_lower[1] = 1
        X = np.zeros((self._N + 1, 2))
        self.solver_wrapper.setup_solver()
        for i in range(self._N + 1):
            X[i, 0] = self.solver_wrapper.solve_stagewise_optim(i, Hzero, g_lower,
                                                                -LARGE, LARGE, -LARGE, LARGE)[1]
            X[i, 1] = self.solver_wrapper.solve_stagewise_optim(i, Hzero, -g_lower,
                                                                -LARGE, LARGE, -LARGE, LARGE)[1]
            logger.debug("X[i]={:}".format(X[i]))
        self.solver_wrapper.close_solver()
        for i in range(self._N + 1):
            if X[i, 0] < 0:
                X[i, 0] = 0
        return X

    def compute_controllable_sets(self, sdmin, sdmax):
        """Compute the sets of controllable squared path velocities.

        Parameters
        ----------
        sdmin: float
            Lower bound on the final path velocity.
        sdmax: float
            Upper bound on the final path velocity.

        Returns
        -------
        K: (N+1,2)array
            K[i] contains the upper and lower bounds of the set of
            controllable squared velocities at position s[i].
        """
        assert sdmin <= sdmax and 0 <= sdmin
        K = np.zeros((self._N + 1, 2))
        K[self._N] = [sdmin ** 2, sdmax ** 2]
        logger.info("Start computing the controllable sets")
        self.solver_wrapper.setup_solver()
        for i in range(self._N - 1, -1, -1):
            K[i] = self._one_step(i, K[i + 1])
            if K[i, 0] < 0:
                K[i, 0] = 0
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[Compute controllable sets] K_{:d}={:}".format(i, K[i]))
        if np.isnan(K[0]).any():
            logger.warn("The 0-th controllable set is empty. This path is not parametrizable.")
        self.solver_wrapper.close_solver()
        return K

    def _one_step(self, i, K_next):
        """ Perform the one-step operation.

        Parameters
        ----------
        i: int
            Stage index.
        K_next: (2,)array
            Two ends of the set of controllable path velocities at stage (i+1).

        Returns
        -------
        res: (2,)array
            Set of controllable squared path velocities K[i].
        """
        res = np.zeros(2)
        if np.isnan(K_next).any() or i < 0 or i > self._N:
            res[:] = np.nan
            return res

        nV = self.solver_wrapper.get_no_vars()
        g_upper = np.zeros(nV)
        g_upper[0] = 1e-9
        g_upper[1] = - 1
        x_upper = self.solver_wrapper.solve_stagewise_optim(
            i, None, g_upper, None, None, K_next[0], K_next[1])[1]
        x_lower = self.solver_wrapper.solve_stagewise_optim(
            i, None, - g_upper, None, None, K_next[0], K_next[1])[1]
        res[:] = [x_lower, x_upper]
        return res

    def compute_parameterization(self, sd_start, sd_end):
        assert sd_end >= 0 and sd_start >= 0, "Path velocities must be positive"
        K = self.compute_controllable_sets(sd_end, sd_end)
        if np.isnan(K).any():
            logger.warn("The set of controllable velocities at the beginning is empty!")
            return None, None, None

        x_start = sd_start ** 2
        if x_start + SMALL < K[0, 0] or K[0, 1] + SMALL < x_start:
            logger.warn("The initial velocity is not controllable. {:f} not in ({:f}, {:f})".format(
                x_start, K[0, 0], K[0, 1]
            ))
            return None, None, None

        N = self.solver_wrapper.get_no_stages()
        deltas = self.solver_wrapper.get_deltas()
        xs = np.zeros(N + 1)
        xs[0] = x_start
        us = np.zeros(N)
        v_vec = np.zeros((N, self.solver_wrapper.get_no_vars() - 2))

        self.solver_wrapper.setup_solver()
        for i in range(self._N):
            optim_res = self._forward_step(i, xs[i], K[i + 1])
            if optim_res[0] is None:
                us[i] = None
                xs[i + 1] = None
                v_vec[i] = None
            else:
                us[i] = optim_res[0]
                # The below function min( , max( ,)) ensure that the state x_{i+1} is controllable.
                # While this is ensured theoretically by the existence of the controllable sets,
                # numerical errors might violate this condition.
                xs[i + 1] = min(K[i + 1, 1], max(K[i + 1, 0], xs[i] + 2 * deltas[i] * us[i]))
                v_vec[i] = optim_res[2:]
            logger.debug("[Forward pass] u_{:d} = {:f}, x_{:d} = {:f}".format(i, us[i], i+1, xs[i+1]))
        self.solver_wrapper.close_solver()
        sd_vec = np.sqrt(xs)
        sdd_vec = np.copy(us)
        return sdd_vec, sd_vec, v_vec

