import logging

import cvxpy
from cvxpy.constraints import NonNeg
import numpy as np
from asgl import ASGL
from sklearn.base import MultiOutputMixin
from sklearn.base import RegressorMixin
from sklearn.linear_model import ElasticNet
from sklearn.linear_model._base import LinearModel
from sklearn.linear_model._coordinate_descent import _alpha_grid
from sklearn.utils import check_X_y
from sklearn.utils.validation import check_is_fitted


class AdaptiveElasticNet(ASGL, ElasticNet, MultiOutputMixin, RegressorMixin):
    """
    Objective function is

        (1 / 2 n_samples) * sum_i ||y_i - y_pred_i||^2
            + alpha * l1ratio * sum_j |coef_j|
            + alpha * (1 - l1ratio) * sum_j w_j * ||coef_j||^2

        w_j = |b_j| ** (-gamma)
        b_j = coefs obtained by fitting ordinary elastic net

        i: sample
        j: feature
        |X|: abs
        ||X||: square norm

    Parameters
    ----------
    - alpha : float, default=1.0
        Constant that multiplies the penalty terms.
    - l1_ratio : float, default=0.5
        float between 0 and 1 passed to ElasticNet (scaling between l1 and l2 penalties).
    - gamma : float > 0, default=1.0
        To guarantee the oracle property, following inequality should be satisfied:
            gamma > 2 * nu / (1 - nu)
            nu = lim(n_samples -> inf) [log(n_features) / log(n_samples)]
        default is 1 because this value is natural in the sense that l1_penalty / l2_penalty
        is not (directly) dependent on scale of features
    - fit_intercept = True
    - positive : bool, default=False
        When set to `True`, forces the coefficients to be positive.

    Examples
    --------
    >>> from sklearn.datasets import make_regression

    # >>> X, y = make_regression(n_features=2, random_state=0)
    # >>> model = AdaptiveElasticNet()
    # >>> model.fit(X, y)
    # AdaptiveElasticNet(solver='default', tol=1e-05)
    # >>> print(model.coef_)
    # [14.24414426 48.9550584 ]
    # >>> print(model.intercept_)
    # 2.092...
    # >>> print(model.predict([[0, 0]]))
    # [2.092...]

    Constraint:
    >>> X, y = make_regression(n_features=10, random_state=0)
    >>> model = AdaptiveElasticNet(positive=True).fit(X, -y)
    >>> print(model.coef_)
    [14.24414426 48.9550584 ]
    >>> print(model.intercept_)
    2.092...
    >>> print(model.predict([[0, 0]]))
    [2.092...]
    """

    def __init__(
        self,
        alpha=1.0,
        *,
        l1_ratio=0.5,
        gamma=1.0,
        fit_intercept=True,
        normalize=False,
        precompute=False,
        copy_X=True,
        eps=1e-3,
        solver=None,
        tol=None,
        positive=False,
    ):
        params_asgl = dict(model="lm", penalization="asgl")
        if solver is not None:
            params_asgl["solver"] = solver
        if tol is not None:
            params_asgl["tol"] = tol

        super().__init__(**params_asgl)

        self.alpha = alpha
        self.l1_ratio = l1_ratio
        self.gamma = gamma
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.precompute = precompute
        self.copy_X = copy_X
        self.eps = eps
        self.positive = positive
        self.positive_tol = 1e-8

        if not self.fit_intercept:
            raise NotImplementedError

        # TODO(simaki) guarantee reproducibility.  is cvxpy reproducible?

    def fit(self, X, y):
        self.coef_, self.intercept_ = self._ae(X, y)

        self.dual_gap_ = np.array([np.nan])
        self.n_iter_ = 1

        return self

    def predict(self, X):
        return super(ElasticNet, self).predict(X)

    def _ae(self, X, y) -> (np.array, float):
        """
        Adaptive elastic-net counterpart of ASGL.asgl

        Returns
        -------
        (coef, intercept)
            - coef : np.array, shape(n_features,)
            - intercept : float
        """
        check_X_y(X, y)

        n_samples, n_features = X.shape
        group_index = np.ones(n_features)
        beta_variables = [cvxpy.Variable(n_features)]
        # _, beta_variables = self._num_beta_var_from_group_index(group_index)
        # beta_variables = cvxpy.Variable()

        model_prediction = 0.0

        if self.fit_intercept:
            beta_variables = [cvxpy.Variable(1)] + beta_variables
            ones = cvxpy.Constant(np.ones((n_samples, 1)))
            model_prediction += ones @ beta_variables[0]

        # --- define objective function ---
        #   l1 weights w_i are identified with coefs in usual elastic net
        #   l2 weights nu_i are fixed to unity in adaptive elastic net

        # /2 * n_samples to make it consistent with sklearn (asgl uses /n_samples)
        model_prediction += X @ beta_variables[1]
        error = cvxpy.sum_squares(y - model_prediction) / (2 * n_samples)

        # XXX: we, paper by Zou Zhang and sklearn use norm squared for l2_penalty whereas asgl uses norm itself
        l1_coefs = self.alpha * self.l1_ratio * self._weights_from_elasticnet(X, y)
        l2_coefs = self.alpha * (1 - self.l1_ratio) * 1.0
        l1_penalty = cvxpy.Constant(l1_coefs) @ cvxpy.abs(beta_variables[1])
        l2_penalty = cvxpy.Constant(l2_coefs) * cvxpy.sum_squares(beta_variables[1])

        constraints = [b >= 0 for b in beta_variables] if self.positive else []

        # --- optimization ---
        problem = cvxpy.Problem(cvxpy.Minimize(error + l1_penalty + l2_penalty), constraints=constraints)
        try:
            if self.solver == "default":
                problem.solve(warm_start=True)
            else:
                solver_dict = self._cvxpy_solver_options(solver=self.solver)
                problem.solve(**solver_dict)
        except (ValueError, cvxpy.error.SolverError):
            logging.warning(
                "Default solver failed. Using alternative options. "
                "Check solver and solver_stats for more details"
            )
            solver = ["ECOS", "OSQP", "SCS"]
            for elt in solver:
                solver_dict = self._cvxpy_solver_options(solver=elt)
                try:
                    problem.solve(**solver_dict)
                    if "optimal" in problem.status:
                        break
                except (ValueError, cvxpy.error.SolverError):
                    continue


        self.solver_stats = problem.solver_stats
        if problem.status in ["infeasible", "unbounded"]:
            logging.warning("Optimization problem status failure")
        beta_sol = np.concatenate([b.value for b in beta_variables], axis=0)
        beta_sol[np.abs(beta_sol) < self.tol] = 0

        intercept, coef = beta_sol[0], beta_sol[1:]

        # Check if constraint violation is less than positive_tol. cf cvxpy issue/#1201
        if self.positive:
            if all(constraint.value(self.positive_tol) for constraint in constraints):
                coef = np.maximum(coef, 0)
            else:
                raise ValueError(f"positive_tol is violated. coef is:\n{coef}")

        return (coef, intercept)

    def _weights_from_elasticnet(self, X, y) -> np.array:
        """
        Determine weighs by fitting ElasticNet

        wj of (2.1) in Zou-Zhang 2009

        Returns
        -------
        weights : np.array, shape (n_features,)
        """
        abscoef = np.maximum(np.abs(ElasticNet().fit(X, y).coef_), self.eps)
        weights = 1 / (abscoef ** self.gamma)

        return weights

    @classmethod
    def aenet_path(
        cls,
        X,
        y,
        *,
        l1_ratio=0.5,
        eps=1e-3,
        n_alphas=100,
        alphas=None,
        precompute="auto",
        Xy=None,
        copy_X=True,
        coef_init=None,
        verbose=False,
        return_n_iter=False,
        positive=False,
        check_input=True,
        **params,
    ):
        """
        Return regression results for multiple alphas

        see enet_path in sklearn

        Returns
        -------
        (alphas, coefs, dual_gaps)
            XXX dual_gaps are nan
        """

        if alphas is None:
            alphas = _alpha_grid(
                X,
                y,
                Xy=Xy,
                l1_ratio=l1_ratio,
                fit_intercept=False,
                eps=eps,
                n_alphas=n_alphas,
                normalize=False,
                copy_X=False,
            )

        n_samples, n_features = X.shape

        dual_gaps = np.empty(n_alphas)
        n_iters = []
        coefs = np.empty((n_features, n_alphas), dtype=X.dtype)
        coef_ = np.zeros(coefs.shape[:-1], dtype=X.dtype, order="F")
        for i, alpha in enumerate(alphas):
            model = cls(alpha=alpha)
            model.fit(X, y)

            coef_ = model.coef_

            coefs[..., i] = coef_
            dual_gaps[i] = np.nan
            n_iters.append(1)

        return (alphas, coefs, dual_gaps)
