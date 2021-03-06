"""High-level decoding object that exposes standard classification and
regression strategies such as SVM, LogisticRegression and Ridge, with optional
feature selection, integrated hyper-parameter selection
and aggregation strategy in which the best models
within a cross validation loop are averaged.
"""
# Authors: Yannick Schwartz
#          Andres Hoyos-Idrobo
#          Binh Nguyen <tuan-binh.nguyen@inria.fr>
#
# License: simplified BSD

import itertools
import warnings

import numpy as np
from sklearn import clone
from sklearn.base import RegressorMixin
from sklearn.linear_model import LogisticRegression
from sklearn.linear_model.base import LinearModel
from sklearn.linear_model.ridge import RidgeCV, RidgeClassifierCV, _BaseRidgeCV
from sklearn.model_selection import ParameterGrid, check_cv, LeaveOneGroupOut
from sklearn.preprocessing import LabelBinarizer
from sklearn.svm import SVR, LinearSVC
from sklearn.svm.bounds import l1_min_c
from sklearn.utils.extmath import safe_sparse_dot
from sklearn.utils.validation import check_is_fitted, check_X_y
from joblib import Parallel, delayed

try:
    from sklearn.metrics import check_scoring
except ImportError:
    # for scikit-learn 0.18 and 0.19
    from sklearn.metrics.scorer import check_scoring

from .._utils import CacheMixin
from .._utils.cache_mixin import _check_memory
from .._utils.param_validation import (_adjust_screening_percentile,
                                       check_feature_screening)
from ..input_data.masker_validation import check_embedded_nifti_masker

SUPPORTED_ESTIMATORS = dict(
    svc_l1=LinearSVC(penalty='l1', dual=False, max_iter=1e4),
    svc_l2=LinearSVC(penalty='l2', max_iter=1e4),
    svc=LinearSVC(penalty='l2', max_iter=1e4),
    logistic_l1=LogisticRegression(penalty='l1', solver='liblinear'),
    logistic_l2=LogisticRegression(penalty='l2', solver='liblinear'),
    logistic=LogisticRegression(penalty='l2', solver='liblinear'),
    ridge_classifier=RidgeClassifierCV(),
    ridge_regressor=RidgeCV(),
    ridge=RidgeCV(),
    svr=SVR(kernel='linear', max_iter=1e4),
)


def _check_param_grid(estimator, X, y, param_grid=None):
    """Check param_grid and return sensible default if param_grid is None.

    Parameters
    -----------
    estimator: str, optional
        The estimator to choose among: 'svc', 'svc_l2', 'svc_l1', 'logistic',
        'logistic_l1', 'logistic_l2', 'ridge', 'ridge_classifier',
        'ridge_regressor', and 'svr'. Note that the 'svc' and 'svc_l2';
        'logistic' and 'logistic_l2'; 'ridge' and 'ridge_regressor'
        correspond to the same estimator. Default 'svc'.

    X: list of Niimg-like objects
        See http://nilearn.github.io/manipulating_images/input_output.html
        Data on which model is to be fitted. If this is a list,
        the affine is considered the same for all.

    y: array or list of shape (n_samples)
        The dependent variable (age, sex, IQ, yes/no, etc.).
        Target variable to predict. Must have exactly as many elements as
        3D images in niimg.

    param_grid: dict of str to sequence, or sequence of such. Default None
        The parameter grid to explore, as a dictionary mapping estimator
        parameters to sequences of allowed values.

        An empty dict signifies default parameters.

        A sequence of dicts signifies a sequence of grids to search, and is
        useful to avoid exploring parameter combinations that make no sense
        or have no effect. See scikit-learn documentation for more information.

    Returns
    -------
    param_grid: dict of str to sequence, or sequence of such. Sensible default
    dict has size 1.

    """
    if param_grid is None:
        param_grid = {}
        # define loss function
        if isinstance(estimator, LogisticRegression):
            loss = 'log'
        elif isinstance(estimator, (LinearSVC, _BaseRidgeCV, SVR)):
            loss = 'squared_hinge'
        else:
            raise ValueError(
                "Invalid estimator. The supported estimators are: {}".format(
                    list(SUPPORTED_ESTIMATORS.keys()))
            )
        # define sensible default for different types of estimators
        if hasattr(estimator, 'penalty') and (estimator.penalty == 'l1'):
            min_c = l1_min_c(X, y, loss=loss)
        else:
            min_c = 0.5

        if not isinstance(estimator, _BaseRidgeCV):
            param_grid['C'] = np.array([2, 20, 200]) * min_c
        else:
            param_grid = {}

    return param_grid


def _check_estimator(estimator):
    if not isinstance(estimator, str):
        warnings.warn('Use a custom estimator at your own risk '
                      'of the process not working as intended.')
    elif estimator in SUPPORTED_ESTIMATORS.keys():
        estimator = SUPPORTED_ESTIMATORS.get(estimator)
    else:
        raise ValueError(
            "Invalid estimator. Known estimators are: {}".format(
                list(SUPPORTED_ESTIMATORS.keys()))
        )

    return estimator


def _parallel_fit(estimator, X, y, train, test, param_grid, is_classification,
                  scorer, mask_img, class_index, screening_percentile=100):
    """Find the best estimator for a fold within a job.
    This function tries several parameters for the estimator for the train and
    test fold provided and save the ones that performs best. These models are
    used afterwards to build the averaged model.

    """
    n_features = X.shape[1]
    # Checking if the size of the mask image allows us to perform feature
    # screening
    selector = check_feature_screening(screening_percentile, mask_img,
                                       is_classification)
    do_screening = (n_features > 100) and selector is not None

    X_train, y_train = X[train], y[train]
    X_test, y_test = X[test], y[test]

    if (selector is not None) and do_screening:
        X_train = selector.fit_transform(X_train, y_train)
        X_test = selector.transform(X_test)

    # If there is no parameter grid, then we use a suitable grid (by default)
    param_grid = _check_param_grid(estimator, X_train, y_train, param_grid)
    best_score = None
    for param in ParameterGrid(param_grid):
        estimator = clone(estimator).set_params(**param)
        estimator.fit(X_train, y_train)

        if is_classification:
            score = scorer(estimator, X_test, y_test)
            if np.all(estimator.coef_ == 0):
                score = 0
        else:  # regression
            score = scorer(estimator, X_test, y_test)

        # Store best parameters and estimator coefficients
        if (best_score is None) or (score >= best_score):
            best_score = score
            best_coef = estimator.coef_
            best_intercept = estimator.intercept_
            best_param = param

    if (selector is not None) and do_screening:
        best_coef = selector.inverse_transform(best_coef)

    return class_index, best_coef, best_intercept, best_param, best_score


class _BaseDecoder(LinearModel, RegressorMixin, CacheMixin):
    """A wrapper for popular classification/regression strategies in
    neuroimaging.

    The `BaseDecoder` object supports classification and regression methods.
    It implements a model selection scheme that averages the best models
    within a cross validation loop (a technique sometimes known as CV bagging).
    The resulting average model is the one used as a classifier or a regressor.
    This object also leverages the `NiftiMaskers` to provide a direct interface
    with the Nifti files on disk.

    Parameters
    -----------
    estimator: str, optional
        The estimator to choose among: 'svc', 'svc_l2', 'svc_l1', 'logistic',
        'logistic_l1', 'logistic_l2', 'ridge', 'ridge_classifier',
        'ridge_regressor', and 'svr'. Note that the 'svc' and 'svc_l2';
        'logistic' and 'logistic_l2'; 'ridge' and 'ridge_regressor'
        correspond to the same estimator. Default 'svc'.

    mask: filename, Nifti1Image, NiftiMasker, or MultiNiftiMasker, optional
        Mask to be used on data. If an instance of masker is passed,
        then its mask and parameters will be used. If no mask is given, mask
        will be computed automatically from provided images by an inbuilt
        masker with default parameters. Refer to NiftiMasker or
        MultiNiftiMasker to check for default parameters. Default None

    cv: cross-validation generator or int, optional. Default 10
        A cross-validation generator.
        See: https://scikit-learn.org/stable/modules/cross_validation.html

    param_grid: dict of str to sequence, or sequence of such. Default None
        The parameter grid to explore, as a dictionary mapping estimator
        parameters to sequences of allowed values.

        None or an empty dict signifies default parameters.

        A sequence of dicts signifies a sequence of grids to search, and is
        useful to avoid exploring parameter combinations that make no sense
        or have no effect. See scikit-learn documentation for more information,
        for example: https://scikit-learn.org/stable/modules/grid_search.html

    screening_percentile: int, float, optional, in the closed interval [0, 100]
        Perform a univariate feature selection based on the Anova F-value for
        the input data. A float according to a percentile of the highest
        scores. Default: 20.

    scoring: str, callable or None, optional. Default None
        The scoring strategy to use. See the scikit-learn documentation at
        https://scikit-learn.org/stable/modules/model_evaluation.html#the-scoring-parameter-defining-model-evaluation-rules
        If callable, takes as arguments the fitted estimator, the
        test data (X_test) and the test target (y_test) if y is
        not None.
        e.g. scorer(estimator, X_test, y_test)

        For regression, valid entries are: 'r2', 'neg_mean_absolute_error', or
        'neg_mean_squared_error'. Default: 'r2'.

        For classification, valid entries are: 'accuracy', 'f1', 'precision',
        'recall' or 'roc_auc'. Default: 'roc_auc'.

    smoothing_fwhm: float, optional. Default: None
        If smoothing_fwhm is not None, it gives the size in millimeters of the
        spatial smoothing to apply to the signal.

    standardize: bool, optional. Default: True
        If standardize is True, the time-series are centered and normed:
        their variance is put to 1 in the time dimension.

    target_affine: 3x3 or 4x4 matrix, optional. Default: None
        This parameter is passed to image.resample_img. Please see the
        related documentation for details.

    target_shape: 3-tuple of int, optional. Default: None
        This parameter is passed to image.resample_img. Please see the
        related documentation for details.

    low_pass: None or float, optional
        This parameter is passed to signal.clean. Please see the related
        documentation for details

    high_pass: None or float, optional
        This parameter is passed to signal.clean. Please see the related
        documentation for details

    t_r: float, optional. Default: None
        This parameter is passed to signal.clean. Please see the related
        documentation for details.

    mask_strategy: {'background' or 'epi'}, optional. Default: 'background'
        The strategy used to compute the mask: use 'background' if your
        images present a clear homogeneous background, and 'epi' if they
        are raw EPI images. Depending on this value, the mask will be
        computed from masking.compute_background_mask or
        masking.compute_epi_mask.

        This parameter will be ignored if a mask image is provided.

    memory: instance of joblib.Memory or str
        Used to cache the masking process.
        By default, no caching is done. If a str is given, it is the
        path to the caching directory.

    memory_level: int, optional. Default: 0
        Rough estimator of the amount of memory used by caching. Higher value
        means more memory for caching.

    n_jobs: int, optional. Default: 1.
        The number of CPUs to use to do the computation. -1 means
        'all CPUs'.

    verbose: int, optional. Default: 0.
        Verbosity level.

    See Also
    ------------
    nilearn.decoding.DecoderRegressor: Regression strategies for Neuroimaging,
    nilearn.decoding.Decoder: Classification strategies for Neuroimaging,
    nilearn.decoding.SpaceNetClassifier: Graph-Net and TV-L1 priors/penalties
    """

    def __init__(self, estimator='svc', mask=None, cv=10, param_grid=None,
                 screening_percentile=20, scoring=None, smoothing_fwhm=None,
                 standardize=True, target_affine=None, target_shape=None,
                 low_pass=None, high_pass=None, t_r=None,
                 mask_strategy='background', is_classification=True,
                 memory=None, memory_level=0, n_jobs=1, verbose=0):
        self.estimator = estimator
        self.mask = mask
        self.cv = cv
        self.param_grid = param_grid
        self.screening_percentile = screening_percentile
        self.scoring = scoring
        self.is_classification = is_classification
        self.smoothing_fwhm = smoothing_fwhm
        self.standardize = standardize
        self.target_affine = target_affine
        self.target_shape = target_shape
        self.mask_strategy = mask_strategy
        self.low_pass = low_pass
        self.high_pass = high_pass
        self.t_r = t_r
        self.memory = memory
        self.memory_level = memory_level
        self.n_jobs = n_jobs
        self.verbose = verbose

    def fit(self, X, y, groups=None):
        """Fit the decoder (learner).

        Parameters
        ----------
        X: list of Niimg-like objects
            See http://nilearn.github.io/manipulating_images/input_output.html
            Data on which model is to be fitted. If this is a list,
            the affine is considered the same for all.

        y: numpy.ndarray of shape=(n_samples) or list of length n_samples
            The dependent variable (age, sex, IQ, yes/no, etc.).
            Target variable to predict. Must have exactly as many elements as
            3D images in niimg.

        groups: None
            Group labels for the samples used while splitting the dataset into
            train/test set. Default None.

            Note that this parameter must be specified in some scikit-learn
            cross-validation generators to calculate the number of splits, e.g.
            sklearn.model_selection.LeaveOneGroupOut or
            sklearn.model_selection.LeavePGroupsOut.

            For more details see
            https://scikit-learn.org/stable/modules/cross_validation.html#cross-validation-iterators-for-grouped-data

        Attributes
        ----------
        `masker_`: instance of NiftiMasker or MultiNiftiMasker
            The NiftiMasker used to mask the data.

        `mask_img_`: Nifti1Image
            Mask computed by the masker object.

        `classes_`: numpy.ndarray
            Classes to predict. For classification only.

        `screening_percentile_`: float
            Screening percentile corrected according to volume of mask,
            relative to the volume of standard brain.

        `coef_`: numpy.ndarray, shape=(n_classes, n_features)
            Contains the mean of the models weight vector across
            fold for each class.

        `coef_img_`: dict of Nifti1Image
            Dictionary containing `coef_` with class names as keys,
            and `coef_` transformed in Nifti1Images as values. In the case of
            a regression, it contains a single Nifti1Image at the key 'beta'.

        `intercept_`: narray, shape (nclasses,)
            Intercept (a.k.a. bias) added to the decision function.

        `cv_`: list of pairs of lists
            List of the (n_folds,) folds. For the corresponding fold,
            each pair is composed of two lists of indices,
            one for the train samples and one for the test samples.

        `std_coef_`: numpy.ndarray, shape=(n_classes, n_features)
            Contains the standard deviation of the models weight vector across
            fold for each class. Note that folds are not independent, see
            https://scikit-learn.org/stable/modules/cross_validation.html#cross-validation-iterators-for-grouped-data

        `std_coef_img_`: dict of Nifti1Image
            Dictionary containing `std_coef_` with class names as keys,
            and `coef_` transformed in Nifti1Image as values. In the case of
            a regression, it contains a single Nifti1Image at the key 'beta'.

        `cv_params_`: dict of lists
            Best point in the parameter grid for each tested fold
            in the inner cross validation loop.

        `cv_scores_`: dict, (classes, n_folds)
            Scores (misclassification) for each parameter, and on each fold
        """
        self.estimator = _check_estimator(self.estimator)
        self.memory_ = _check_memory(self.memory, self.verbose)

        X = self._apply_mask(X)
        X, y = check_X_y(X, y, dtype=np.float, multi_output=True)

        # Setup scorer
        scorer = check_scoring(self.estimator, self.scoring)

        # Setup cross-validation object. Default is StratifiedKFold when groups
        # is None. If groups is specified but self.cv is not set to custom CV
        # splitter, default is LeaveOneGroupOut. If self.cv is manually set to
        # a CV splitter object do check_cv regardless of groups parameter.
        cv = self.cv
        if (isinstance(cv, int) or cv is None) and groups is not None:
            warnings.warn(
                'groups parameter is specified but '
                'cv parameter is not set to custom CV splitter. '
                'Using default object LeaveOneGroupOut().'
            )
            cv_object = LeaveOneGroupOut()
        else:
            cv_object = check_cv(cv, y=y, classifier=self.is_classification)

        self.cv_ = list(cv_object.split(X, y, groups=groups))

        # Define the number problems to solve. In case of classification this
        # number corresponds to the number of binary problems to solve
        if self.is_classification:
            y = self._binarize_y(y)
        else:
            y = y[:, np.newaxis]
        if self.is_classification and self.n_classes_ > 2:
            n_problems = self.n_classes_
        else:
            n_problems = 1

        # Return a suitable screening percentile according to the mask image
        self.screening_percentile_ = _adjust_screening_percentile(
            self.screening_percentile, self.mask_img_, verbose=self.verbose)

        parallel = Parallel(n_jobs=self.n_jobs, verbose=2 * self.verbose)

        parallel_fit_outputs = parallel(
            delayed(self._cache(_parallel_fit))(
                self.estimator, X, y[:, c], train, test,
                self.param_grid, self.is_classification, scorer,
                self.mask_img_, c, self.screening_percentile_)
            for c, (train, test) in itertools.product(
                range(n_problems), self.cv_))

        coefs, intercepts = self._fetch_parallel_fit_outputs(
            parallel_fit_outputs, y, n_problems)

        # Build the final model (the aggregated one)
        self.coef_ = np.vstack([np.mean(coefs[class_index], axis=0)
                                for class_index in self.classes_])
        self.std_coef_ = np.vstack([np.std(coefs[class_index], axis=0)
                                   for class_index in self.classes_])
        self.intercept_ = np.hstack([np.mean(intercepts[class_index], axis=0)
                                    for class_index in self.classes_])

        self.coef_img_, self.std_coef_img_ = self._output_image(
            self.classes_, self.coef_, self.std_coef_)

        if self.is_classification and (self.n_classes_ == 2):
            self.coef_ = self.coef_[0, :][np.newaxis, :]
            self.intercept_ = self.intercept_[0]

    def decision_function(self, X):
        """Predict class labels for samples in X.

        Parameters
        ----------
        X: list of Niimg-like objects
            See
            <http://nilearn.github.io/manipulating_images/input_output.html>
            Data on prediction is to be made. If this is a list,
            the affine is considered the same for all.

        Returns
        -------
        y_pred: ndarray, shape (n_samples,)
            Predicted class label per sample.
        """
        X = self.masker_.transform(X)

        n_features = self.coef_.shape[1]
        if X.shape[1] != n_features:
            raise ValueError(
                "X has {} features per sample; expecting {}".format(
                    (X.shape[1], n_features)))

        scores = safe_sparse_dot(X, self.coef_.T,
                                 dense_output=True) + self.intercept_

        return scores.ravel() if scores.shape[1] == 1 else scores

    def predict(self, X):
        """Predict a label for all X vectors indexed by the first axis.

        Parameters
        ----------
        X: {array-like, sparse matrix}, shape = (n_samples, n_features)
            Samples.

        Returns
        -------
        array, shape=(n_samples,) if n_classes == 2 else (n_samples, n_classes)
            Confidence scores per (sample, class) combination. In the binary
            case, confidence score for self.classes_[1] where >0 means this
            class would be predicted.
        """

        check_is_fitted(self, "coef_")
        check_is_fitted(self, "masker_")

        scores = self.decision_function(X)

        if self.is_classification:
            if len(scores.shape) == 1:
                indices = (scores > 0).astype(np.int)
            else:
                indices = scores.argmax(axis=1)
            return self.classes_[indices]

        return scores

    def _apply_mask(self, X):
        # Nifti masking
        self.masker_ = check_embedded_nifti_masker(self, multi_subject=False)
        X = self.masker_.fit_transform(X)
        self.mask_img_ = self.masker_.mask_img_

        return X

    def _fetch_parallel_fit_outputs(self, parallel_fit_outputs, y, n_problems):
        """Fetch the outputs from parallel_fit to be ready for ensembling

        Parameters
        ----------

        parallel_fit_outputs : list of tuples, each tuple contains results of
            one _parallel_fit for each cv fold (and each classification in the
            case of multiclass classification).

        y : ndarray, shape = (n_samples, )
            Vector of responses.

        Returns
        -------

        coefs : dict
            Coefficients for each classification/regression problem
        intercepts : dict
            Intercept for each classification/regression problem
        """

        coefs = {}
        intercepts = {}
        cv_scores = {}
        self.cv_params_ = {}
        classes = self.classes_

        for i, (class_index, coef, intercept, params,
                scores) in enumerate(parallel_fit_outputs):

            coefs.setdefault(classes[class_index], []).append(coef)
            intercepts.setdefault(classes[class_index], []).append(intercept)

            cv_scores.setdefault(classes[class_index], []).append(scores)

            self.cv_params_.setdefault(classes[class_index], {})
            for k in params:
                self.cv_params_[classes[class_index]].setdefault(
                    k, []).append(params[k])

            if (n_problems <= 2) and self.is_classification:
                # Binary classification
                other_class = np.setdiff1d(classes, classes[class_index])[0]
                coefs.setdefault(other_class, []).append(-coef)
                intercepts.setdefault(other_class, []).append(-intercept)
                cv_scores.setdefault(other_class, []).append(scores)
                self.cv_params_[other_class] = self.cv_params_[
                    classes[class_index]]

        self.cv_scores_ = cv_scores

        return coefs, intercepts

    def _output_image(self, classes, coefs, std_coef):
        coef_img = {}
        std_coef_img = {}
        for class_index, coef, std in zip(classes, coefs, std_coef):
            coef_img[class_index] = self.masker_.inverse_transform(coef)
            std_coef_img[class_index] = self.masker_.inverse_transform(std)

        return coef_img, std_coef_img


class Decoder(_BaseDecoder):
    """A wrapper for popular classification strategies in neuroimaging.

    The `Decoder` object supports classification methods.
    It implements a model selection scheme that averages the best models
    within a cross validation loop. The resulting average model is the
    one used as a classifier. This object also leverages the`NiftiMaskers` to
    provide a direct interface with the Nifti files on disk.

    Parameters
    -----------
    estimator: str, optional
        The estimator to choose among: 'svc', 'svc_l2', 'svc_l1', 'logistic',
        'logistic_l1', 'logistic_l2' and 'ridge_classifier'. Note that
        'svc' and 'svc_l2'; 'logistic' and 'logistic_l2' correspond to the same
        estimator. Default 'svc'.

    mask: filename, Nifti1Image, NiftiMasker, or MultiNiftiMasker, optional
        Mask to be used on data. If an instance of masker is passed,
        then its mask and parameters will be used. If no mask is given, mask
        will be computed automatically from provided images by an inbuilt
        masker with default parameters. Refer to NiftiMasker or
        MultiNiftiMasker to check for default parameters. Default None

    cv: cross-validation generator or int, optional (default 10)
        A cross-validation generator.
        See: https://scikit-learn.org/stable/modules/cross_validation.html

    param_grid: dict of str to sequence, or sequence of such. Default None
        The parameter grid to explore, as a dictionary mapping estimator
        parameters to sequences of allowed values.

        None or an empty dict signifies default parameters.

        A sequence of dicts signifies a sequence of grids to search, and is
        useful to avoid exploring parameter combinations that make no sense
        or have no effect. See scikit-learn documentation for more information,
        for example: https://scikit-learn.org/stable/modules/grid_search.html

    screening_percentile: int, float, optional, in the closed interval [0, 100]
        Perform an univariate feature selection based on the Anova F-value for
        the input data. A float according to a percentile of the highest
        scores. Default: 20.

    scoring: str, callable or None, optional. Default: 'roc_auc'
        The scoring strategy to use. See the scikit-learn documentation at
        https://scikit-learn.org/stable/modules/model_evaluation.html#the-scoring-parameter-defining-model-evaluation-rules
        If callable, takes as arguments the fitted estimator, the
        test data (X_test) and the test target (y_test) if y is
        not None.
        e.g. scorer(estimator, X_test, y_test)

        For classification, valid entries are: 'accuracy', 'f1', 'precision',
        'recall' or 'roc_auc'. Default: 'roc_auc'.

    smoothing_fwhm: float, optional. Default: None
        If smoothing_fwhm is not None, it gives the size in millimeters of the
        spatial smoothing to apply to the signal.

    standardize: bool, optional. Default: True
        If standardize is True, the time-series are centered and normed:
        their variance is put to 1 in the time dimension.

    target_affine: 3x3 or 4x4 matrix, optional. Default: None
        This parameter is passed to image.resample_img. Please see the
        related documentation for details.

    target_shape: 3-tuple of int, optional. Default: None
        This parameter is passed to image.resample_img. Please see the
        related documentation for details.

    low_pass: None or float, optional
        This parameter is passed to signal.clean. Please see the related
        documentation for details

    high_pass: None or float, optional
        This parameter is passed to signal.clean. Please see the related
        documentation for details

    t_r: float, optional. Default: None
        This parameter is passed to signal.clean. Please see the related
        documentation for details.

    mask_strategy: {'background' or 'epi'}, optional. Default: 'background'
        The strategy used to compute the mask: use 'background' if your
        images present a clear homogeneous background, and 'epi' if they
        are raw EPI images. Depending on this value, the mask will be
        computed from masking.compute_background_mask or
        masking.compute_epi_mask.

        This parameter will be ignored if a mask image is provided.

    memory: instance of joblib.Memory or str
        Used to cache the masking process.
        By default, no caching is done. If a str is given, it is the
        path to the caching directory.

    memory_level: int, optional. Default: 0
        Rough estimator of the amount of memory used by caching. Higher value
        means more memory for caching.

    n_jobs: int, optional. Default: 1.
        The number of CPUs to use to do the computation. -1 means
        'all CPUs'.

    verbose: int, optional. Default: 0.
        Verbosity level.

    See Also
    ------------
    nilearn.decoding.DecoderRegressor: regression strategies for Neuro-imaging,
    nilearn.decoding.SpaceNetClassifier: Graph-Net and TV-L1 priors/penalties
    """

    def __init__(self, estimator='svc', mask=None, cv=10, param_grid=None,
                 screening_percentile=20, scoring='roc_auc',
                 smoothing_fwhm=None, standardize=True, target_affine=None,
                 target_shape=None, mask_strategy='background',
                 low_pass=None, high_pass=None, t_r=None, memory=None,
                 memory_level=0, n_jobs=1, verbose=0):
        super().__init__(
            estimator=estimator, mask=mask, cv=cv, param_grid=param_grid,
            screening_percentile=screening_percentile, scoring=scoring,
            smoothing_fwhm=smoothing_fwhm, standardize=standardize,
            target_affine=target_affine, target_shape=target_shape,
            mask_strategy=mask_strategy, low_pass=low_pass,
            high_pass=high_pass, t_r=t_r, memory=memory,
            is_classification=True, memory_level=memory_level,
            verbose=verbose, n_jobs=n_jobs)

    def _binarize_y(self, y):
        """Helper function invoked just before fitting a classifier."""
        y = np.array(y)

        # encode target classes as -1 and 1
        self._enc = LabelBinarizer(pos_label=1, neg_label=-1)
        y = self._enc.fit_transform(y)
        self.classes_ = self._enc.classes_
        self.n_classes_ = len(self.classes_)
        return y


class DecoderRegressor(_BaseDecoder):
    """A wrapper for popular regression strategies in neuroimaging.

    The `DecoderRegressor` object supports regression methods.
    It implements a model selection scheme that averages the best models
    within a cross validation loop. The resulting average model is the
    one used as a regressor. This object also leverages the `NiftiMaskers`
    to provide a direct interface with the Nifti files on disk.

    Parameters
    -----------
    estimator: str, optional
        The estimator to choose among: 'ridge', 'ridge_regressor', and 'svr'.
        Note that the 'ridge' and 'ridge_regressor' correspond to the same
        estimator. Default 'svr'.

    mask: filename, Nifti1Image, NiftiMasker, or MultiNiftiMasker, optional
        Mask to be used on data. If an instance of masker is passed,
        then its mask and parameters will be used. If no mask is given, mask
        will be computed automatically from provided images by an inbuilt
        masker with default parameters. Refer to NiftiMasker or
        MultiNiftiMasker to check for default parameters. Default None

    cv: cross-validation generator or int, optional (default 10)
        A cross-validation generator.
        See: https://scikit-learn.org/stable/modules/cross_validation.html

    param_grid: dict of str to sequence, or sequence of such. Default None
        The parameter grid to explore, as a dictionary mapping estimator
        parameters to sequences of allowed values.

        None or an empty dict signifies default parameters.

        A sequence of dicts signifies a sequence of grids to search, and is
        useful to avoid exploring parameter combinations that make no sense
        or have no effect. See scikit-learn documentation for more information,
        for example: https://scikit-learn.org/stable/modules/grid_search.html

    screening_percentile: int, float, optional, in the closed interval [0, 100]
        Perform a univariate feature selection based on the Anova F-value for
        the input data. A float according to a percentile of the highest
        scores. Default: 20.

    scoring: str, callable or None, optional. Default: 'r2'
        The scoring strategy to use. See the scikit-learn documentation at
        https://scikit-learn.org/stable/modules/model_evaluation.html#the-scoring-parameter-defining-model-evaluation-rules
        If callable, takes as arguments the fitted estimator, the
        test data (X_test) and the test target (y_test) if y is
        not None.
        e.g. scorer(estimator, X_test, y_test)

        For regression, valid entries are: 'r2', 'neg_mean_absolute_error',
        or 'neg_mean_squared_error'. Default: 'r2'.

    smoothing_fwhm: float, optional. Default: None
        If smoothing_fwhm is not None, it gives the size in millimeters of the
        spatial smoothing to apply to the signal.

    standardize: bool, optional. Default: True
        If standardize is True, the time-series are centered and normed:
        their variance is put to 1 in the time dimension.

    target_affine: 3x3 or 4x4 matrix, optional. Default: None
        This parameter is passed to image.resample_img. Please see the
        related documentation for details.

    target_shape: 3-tuple of int, optional. Default: None
        This parameter is passed to image.resample_img. Please see the
        related documentation for details.

    low_pass: None or float, optional
        This parameter is passed to signal.clean. Please see the related
        documentation for details

    high_pass: None or float, optional
        This parameter is passed to signal.clean. Please see the related
        documentation for details

    t_r: float, optional. Default: None
        This parameter is passed to signal.clean. Please see the related
        documentation for details.

    mask_strategy: {'background' or 'epi'}, optional. Default: 'background'
        The strategy used to compute the mask: use 'background' if your
        images present a clear homogeneous background, and 'epi' if they
        are raw EPI images. Depending on this value, the mask will be
        computed from masking.compute_background_mask or
        masking.compute_epi_mask.

        This parameter will be ignored if a mask image is provided.

    memory: instance of joblib.Memory or str
        Used to cache the masking process.
        By default, no caching is done. If a str is given, it is the
        path to the caching directory.

    memory_level: int, optional. Default: 0
        Rough estimator of the amount of memory used by caching. Higher value
        means more memory for caching.

    n_jobs: int, optional. Default: 1.
        The number of CPUs to use to do the computation. -1 means
        'all CPUs'.

    verbose: int, optional. Default: 0.
        Verbosity level.

    See Also
    ------------
    nilearn.decoding.Decoder: classification strategies for Neuroimaging,
    nilearn.decoding.SpaceNetClassifier: Graph-Net and TV-L1 priors/penalties
    """

    def __init__(self, estimator='svr', mask=None, cv=10, param_grid=None,
                 screening_percentile=20, scoring='r2',
                 smoothing_fwhm=None, standardize=True, target_affine=None,
                 target_shape=None, mask_strategy='background',
                 low_pass=None, high_pass=None, t_r=None, memory=None,
                 memory_level=0, n_jobs=1, verbose=0):
        self.classes_ = ['beta']

        super().__init__(
            estimator=estimator, mask=mask, cv=cv, param_grid=param_grid,
            screening_percentile=screening_percentile, scoring=scoring,
            smoothing_fwhm=smoothing_fwhm, standardize=standardize,
            target_affine=target_affine, target_shape=target_shape,
            low_pass=low_pass, high_pass=high_pass, t_r=t_r,
            mask_strategy=mask_strategy, memory=memory,
            is_classification=False, memory_level=memory_level,
            verbose=verbose, n_jobs=n_jobs)
