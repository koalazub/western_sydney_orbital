"""Frame-consistent calibration and covariance reference math.

The 20 October 2026 state-estimation discussion calls for bias and scale
handling, residual and process uncertainty, and a prediction/update cycle
without selecting a sensor model or parameters. This module supplies local
reference calculations for those steps. It is not STP evidence, a calibrated
sensor probability, a flight recommendation, or a replacement for the existing
state estimator.

Assumptions:

- Frame labels are opaque caller-supplied identifiers. They must match exactly
  before paired data or covariances are combined. No rotation, translation or
  clock conversion is inferred.
- The measurement model is per-channel affine:
  ``measured = scale * reference + bias + noise``. Channels do not mix.
- Calibration samples are paired at the supplied times and are in one frame.
- Residual and process covariances use the sample covariance with an ``N - 1``
  denominator. Regularization, eigenvalue floors and conditioning limits are
  explicit inputs or documented defaults.
- Process noise is estimated from residuals of a caller-supplied linear
  transition model, not from an inferred STP dynamics model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Any

import numpy as np
import numpy.typing as npt

from ._validation import real_array

type FloatArray = npt.NDArray[np.float64]

_MIN_CALIBRATION_SAMPLES = 3
_MIN_PROCESS_RESIDUALS = 3
_DEFAULT_EIGENVALUE_FLOOR = 1e-12
_DEFAULT_CONDITION_LIMIT = 1e12

__all__ = [
    "CovarianceRegularization",
    "CovarianceUpdate",
    "FrameCovariance",
    "MeasurementCalibration",
    "ProcessNoiseEstimate",
    "estimate_process_noise",
    "fit_measurement_calibration",
    "predict_covariance",
    "regularize_covariance",
    "update_covariance",
]


def _label(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty label")
    return value


def _finite_nonnegative(value: Any, name: str) -> float:
    message = f"{name} must be a finite real number >= 0"
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(message)
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(message) from error
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(message)
    return number


def _condition_limit(value: Any) -> float:
    limit = _finite_nonnegative(value, "condition_limit")
    if limit <= 1.0:
        raise ValueError("condition_limit must be greater than 1")
    return limit


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1")
    return int(value)


def _samples(value: Any, name: str) -> tuple[FloatArray, bool]:
    array = real_array(value, name)
    if array.ndim == 1:
        return array.reshape(-1, 1), True
    if array.ndim == 2 and array.shape[1] >= 1:
        return array, False
    raise ValueError(f"{name} must have shape (N,) or (N, channels) with channels >= 1")


def _time_series(value: Any) -> FloatArray:
    time_s = real_array(value, "time_s")
    if time_s.ndim != 1:
        raise ValueError("time_s must have shape (N,)")
    if time_s.size < 2:
        raise ValueError("time_s must contain at least two samples")
    if np.any(np.diff(time_s) <= 0.0):
        raise ValueError("time_s must be strictly increasing")
    return time_s


def _square_matrix(value: Any, name: str) -> FloatArray:
    matrix = real_array(value, name)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1] or matrix.shape[0] == 0:
        raise ValueError(f"{name} must be a non-empty square matrix")
    return matrix


def _symmetric_matrix(value: Any, name: str) -> FloatArray:
    matrix = _square_matrix(value, name)
    scale = max(1.0, float(np.max(np.abs(matrix))))
    if not np.allclose(matrix, matrix.T, rtol=1e-10, atol=1e-12 * scale):
        raise ValueError(f"{name} must be symmetric")
    return 0.5 * (matrix + matrix.T)


def _positive_definite(value: Any, name: str, condition_limit: float) -> FloatArray:
    matrix = _symmetric_matrix(value, name)
    eigenvalues = np.linalg.eigvalsh(matrix)
    smallest = float(eigenvalues[0])
    largest = float(eigenvalues[-1])
    if not (math.isfinite(smallest) and math.isfinite(largest)) or smallest <= 0.0:
        raise ValueError(f"{name} must be positive definite and finite")
    condition = largest / smallest
    if condition > condition_limit:
        raise ValueError(f"{name} is ill-conditioned ({condition:.3g} > {condition_limit:.3g})")
    return matrix


def _require_frame_covariance(value: Any, name: str) -> FrameCovariance:
    if not isinstance(value, FrameCovariance):
        raise ValueError(f"{name} must be a FrameCovariance")
    return value


def _require_same_metadata(
    left: FrameCovariance,
    right: FrameCovariance,
    left_name: str,
    right_name: str,
) -> None:
    if left.frame != right.frame:
        raise ValueError(
            f"{left_name} frame {left.frame!r} does not match {right_name} frame {right.frame!r}"
        )
    if left.timebase != right.timebase:
        raise ValueError(
            f"{left_name} timebase {left.timebase!r} does not match "
            f"{right_name} timebase {right.timebase!r}"
        )


@dataclass(frozen=True, slots=True)
class FrameCovariance:
    """Symmetric covariance values in one explicit frame and timebase.

    Construction checks shape, finiteness and symmetry. Positive definiteness
    and conditioning are checked by the operation that consumes the matrix,
    because the acceptable limit is operation-specific. Labels are opaque and
    must match exactly. This is local reference math, not STP evidence.
    """

    frame: str
    timebase: str
    matrix: FloatArray

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _label(self.frame, "frame"))
        object.__setattr__(self, "timebase", _label(self.timebase, "timebase"))
        object.__setattr__(self, "matrix", _symmetric_matrix(self.matrix, "covariance"))


@dataclass(frozen=True, slots=True)
class CovarianceRegularization:
    """Diagnostics for an explicitly regularized covariance matrix.

    ``clipped_eigenvalues`` counts loaded eigenvalues below the requested
    floor, including negative sampled values. This is local reference math,
    not a calibrated uncertainty statement or STP evidence.
    """

    matrix: FloatArray
    diagonal_loading: float
    eigenvalue_floor: float
    clipped_eigenvalues: int
    min_eigenvalue_before: float
    min_eigenvalue_after: float
    condition_number_before: float
    condition_number_after: float


@dataclass(frozen=True, slots=True)
class MeasurementCalibration:
    """Per-channel affine calibration and regularized residual covariance.

    ``bias`` and ``scale`` are vectors in measurement-channel order. Applying
    the correction uses ``(measurement - bias) / scale``. The residual
    covariance is in measurement units; ``corrected_covariance`` converts it
    to reference units with the inverse scale. This is local reference math,
    not calibrated sensor probabilities or STP evidence.
    """

    frame: str
    timebase: str
    bias: FloatArray
    scale: FloatArray
    residual_covariance: FrameCovariance
    sample_count: int
    regularization: CovarianceRegularization

    def __post_init__(self) -> None:
        frame = _label(self.frame, "frame")
        timebase = _label(self.timebase, "timebase")
        bias = real_array(self.bias, "bias")
        scale = real_array(self.scale, "scale")
        if bias.ndim != 1 or scale.ndim != 1 or bias.shape != scale.shape or bias.size == 0:
            raise ValueError("bias and scale must be non-empty vectors of equal length")
        if np.any(np.abs(scale) <= np.finfo(np.float64).tiny):
            raise ValueError("scale must be nonzero and invertible")
        if not isinstance(self.residual_covariance, FrameCovariance):
            raise ValueError("residual_covariance must be a FrameCovariance")
        if self.residual_covariance.frame != frame or self.residual_covariance.timebase != timebase:
            raise ValueError("residual covariance labels must match the calibration labels")
        if self.residual_covariance.matrix.shape != (bias.size, bias.size):
            raise ValueError("residual covariance shape must match the channel count")
        count = _positive_integer(self.sample_count, "sample_count")
        if not isinstance(self.regularization, CovarianceRegularization):
            raise ValueError("regularization must be a CovarianceRegularization")
        object.__setattr__(self, "frame", frame)
        object.__setattr__(self, "timebase", timebase)
        object.__setattr__(self, "bias", bias)
        object.__setattr__(self, "scale", scale)
        object.__setattr__(self, "sample_count", count)

    def correct(self, measurements: npt.ArrayLike, *, frame: str) -> FloatArray:
        """Apply the fitted affine correction to scalar or vector samples.

        A one-dimensional input is treated as ``N`` scalar samples and returns
        one-dimensional output. A two-dimensional input is ``(N, channels)``.
        The caller must pass the measurement frame explicitly. This is local
        reference math, not STP evidence.
        """
        measurement_frame = _label(frame, "frame")
        if measurement_frame != self.frame:
            raise ValueError(
                f"measurement frame {measurement_frame!r} does not match "
                f"calibration frame {self.frame!r}"
            )
        values, was_1d = _samples(measurements, "measurements")
        if values.shape[1] != self.bias.size:
            raise ValueError("measurement channel count does not match the calibration")
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            corrected = (values - self.bias) / self.scale
        if not np.isfinite(corrected).all():
            raise ValueError("corrected measurements are not finite")
        return corrected[:, 0] if was_1d else corrected

    def corrected_covariance(self) -> FrameCovariance:
        """Return residual covariance mapped into reference units.

        The transform is ``diag(1 / scale) R diag(1 / scale)``. This is local
        reference math, not STP evidence.
        """
        inverse = 1.0 / self.scale
        with np.errstate(over="ignore", invalid="ignore"):
            matrix = self.residual_covariance.matrix * inverse[:, None] * inverse[None, :]
        if not np.isfinite(matrix).all():
            raise ValueError("corrected covariance is not finite")
        return FrameCovariance(self.frame, self.timebase, matrix)


@dataclass(frozen=True, slots=True)
class ProcessNoiseEstimate:
    """Sample process-noise covariance from consecutive state residuals.

    The covariance is the discrete per-transition covariance of
    ``x[k + 1] - F[k] x[k]`` under the caller's model. It is local reference
    math, not a calibrated STP process model or evidence.
    """

    covariance: FrameCovariance
    sample_count: int
    mean_residual: FloatArray
    regularization: CovarianceRegularization

    def __post_init__(self) -> None:
        if not isinstance(self.covariance, FrameCovariance):
            raise ValueError("covariance must be a FrameCovariance")
        mean_residual = real_array(self.mean_residual, "mean_residual")
        if mean_residual.ndim != 1 or mean_residual.size != self.covariance.matrix.shape[0]:
            raise ValueError("mean_residual must match the state dimension")
        count = _positive_integer(self.sample_count, "sample_count")
        if not isinstance(self.regularization, CovarianceRegularization):
            raise ValueError("regularization must be a CovarianceRegularization")
        object.__setattr__(self, "mean_residual", mean_residual)
        object.__setattr__(self, "sample_count", count)

    @property
    def frame(self) -> str:
        return self.covariance.frame

    @property
    def timebase(self) -> str:
        return self.covariance.timebase


@dataclass(frozen=True, slots=True)
class CovarianceUpdate:
    """Linear Kalman covariance update result.

    The posterior uses the Joseph form. Frames and timebases are those of the
    prior and measurement matrices supplied to ``update_covariance``. This is
    local reference math, not STP evidence.
    """

    posterior: FrameCovariance
    gain: FloatArray
    innovation_covariance: FloatArray

    def __post_init__(self) -> None:
        if not isinstance(self.posterior, FrameCovariance):
            raise ValueError("posterior must be a FrameCovariance")
        gain = real_array(self.gain, "gain")
        innovation = _symmetric_matrix(self.innovation_covariance, "innovation_covariance")
        if gain.ndim != 2:
            raise ValueError("gain must have shape (state_dimension, measurement_dimension)")
        expected = (self.posterior.matrix.shape[0], innovation.shape[0])
        if gain.shape != expected:
            raise ValueError(f"gain shape must be {expected}")
        object.__setattr__(self, "gain", gain)
        object.__setattr__(self, "innovation_covariance", innovation)


def regularize_covariance(
    covariance: npt.ArrayLike,
    *,
    diagonal_loading: float = 0.0,
    eigenvalue_floor: float = 0.0,
    condition_limit: float = _DEFAULT_CONDITION_LIMIT,
) -> CovarianceRegularization:
    """Symmetrize, load and eigenvalue-clip a covariance matrix.

    ``diagonal_loading`` is added to every diagonal entry. Loaded eigenvalues
    below ``eigenvalue_floor`` are replaced by that floor, so negative sampled
    eigenvalues are clipped explicitly rather than hidden. A zero floor with a
    singular matrix is rejected, as is a result above ``condition_limit``.
    The input must already be symmetric. This is local reference math, not STP
    evidence or a calibrated uncertainty statement.
    """
    loading = _finite_nonnegative(diagonal_loading, "diagonal_loading")
    floor = _finite_nonnegative(eigenvalue_floor, "eigenvalue_floor")
    limit = _condition_limit(condition_limit)
    symmetric = _symmetric_matrix(covariance, "covariance")

    before = np.linalg.eigvalsh(symmetric)
    min_before = float(before[0])
    condition_before = float(before[-1] / before[0]) if before[0] > 0.0 else math.inf

    loaded = symmetric + loading * np.eye(symmetric.shape[0])
    eigenvalues, eigenvectors = np.linalg.eigh(loaded)
    clipped = int(np.count_nonzero(eigenvalues < floor))
    adjusted = np.maximum(eigenvalues, floor)
    matrix = (eigenvectors * adjusted) @ eigenvectors.T
    matrix = 0.5 * (matrix + matrix.T)

    after = np.linalg.eigvalsh(matrix)
    min_after = float(after[0])
    max_after = float(after[-1])
    if not (math.isfinite(min_after) and math.isfinite(max_after)) or min_after <= 0.0:
        raise ValueError(
            "covariance remains singular after regularization; "
            "provide a positive eigenvalue_floor or diagonal_loading"
        )
    condition_after = max_after / min_after
    if condition_after > limit:
        raise ValueError(
            "covariance is ill-conditioned after regularization "
            f"({condition_after:.3g} > {limit:.3g})"
        )
    return CovarianceRegularization(
        matrix=matrix,
        diagonal_loading=loading,
        eigenvalue_floor=floor,
        clipped_eigenvalues=clipped,
        min_eigenvalue_before=min_before,
        min_eigenvalue_after=min_after,
        condition_number_before=condition_before,
        condition_number_after=condition_after,
    )


def fit_measurement_calibration(
    measured: npt.ArrayLike,
    reference: npt.ArrayLike,
    time_s: npt.ArrayLike,
    *,
    measurement_frame: str,
    reference_frame: str,
    timebase: str,
    diagonal_loading: float = 0.0,
    eigenvalue_floor: float = _DEFAULT_EIGENVALUE_FLOOR,
    condition_limit: float = _DEFAULT_CONDITION_LIMIT,
    minimum_absolute_scale: float = 0.0,
) -> MeasurementCalibration:
    """Fit per-channel scale, bias and residual covariance from paired data.

    Samples are ``(N,)`` scalar series or ``(N, channels)`` vector series;
    measured and reference must have identical shapes and times. Measurement
    and reference frame labels must match exactly. The caller supplies the
    timebase and ordering; no STP clock or frame is inferred. The covariance
    is the ``N - 1`` sample covariance of the post-fit residuals, then
    regularized. This is local reference math, not STP evidence or a claim
    about calibrated sensor probabilities.
    """
    measurement_frame = _label(measurement_frame, "measurement_frame")
    reference_frame = _label(reference_frame, "reference_frame")
    timebase = _label(timebase, "timebase")
    if measurement_frame != reference_frame:
        raise ValueError(
            f"measurement frame {measurement_frame!r} does not match "
            f"reference frame {reference_frame!r}; transform one series first"
        )
    loading = _finite_nonnegative(diagonal_loading, "diagonal_loading")
    floor = _finite_nonnegative(eigenvalue_floor, "eigenvalue_floor")
    limit = _condition_limit(condition_limit)
    min_scale = _finite_nonnegative(minimum_absolute_scale, "minimum_absolute_scale")

    measured_values = real_array(measured, "measured")
    reference_values = real_array(reference, "reference")
    if measured_values.shape != reference_values.shape:
        raise ValueError("measured and reference must have identical shapes")
    measured_samples, _ = _samples(measured_values, "measured")
    reference_samples, _ = _samples(reference_values, "reference")
    sample_count = measured_samples.shape[0]
    if sample_count < _MIN_CALIBRATION_SAMPLES:
        raise ValueError(f"at least {_MIN_CALIBRATION_SAMPLES} paired samples are required")

    times = _time_series(time_s)
    if times.size != sample_count:
        raise ValueError("time_s length must match the sample count")

    reference_mean = reference_samples.mean(axis=0)
    reference_centered = reference_samples - reference_mean
    reference_energy = np.einsum("ij,ij->j", reference_centered, reference_centered, optimize=True)
    centered_scale = np.max(np.abs(reference_centered), axis=0)
    threshold = np.finfo(np.float64).eps * sample_count * centered_scale * centered_scale
    if np.any(reference_energy <= threshold):
        raise ValueError("reference samples do not vary enough to identify every scale")

    measured_mean = measured_samples.mean(axis=0)
    measured_centered = measured_samples - measured_mean
    scale = (
        np.einsum("ij,ij->j", reference_centered, measured_centered, optimize=True)
        / reference_energy
    )
    bias = measured_mean - scale * reference_mean
    if not (np.isfinite(scale).all() and np.isfinite(bias).all()):
        raise ValueError("calibration fit is not finite")
    if np.any(np.abs(scale) <= max(min_scale, np.finfo(np.float64).tiny)):
        raise ValueError("fitted scale is zero or too small to invert")

    residuals = measured_samples - (reference_samples * scale + bias)
    raw_covariance = residuals.T @ residuals / (sample_count - 1)
    if not np.isfinite(raw_covariance).all():
        raise ValueError("residual covariance is not finite")
    regularization = regularize_covariance(
        raw_covariance,
        diagonal_loading=loading,
        eigenvalue_floor=floor,
        condition_limit=limit,
    )
    covariance = FrameCovariance(measurement_frame, timebase, regularization.matrix)
    return MeasurementCalibration(
        frame=measurement_frame,
        timebase=timebase,
        bias=bias,
        scale=scale,
        residual_covariance=covariance,
        sample_count=sample_count,
        regularization=regularization,
    )


def estimate_process_noise(
    time_s: npt.ArrayLike,
    states: npt.ArrayLike,
    transition_matrices: npt.ArrayLike,
    *,
    frame: str,
    timebase: str,
    diagonal_loading: float = 0.0,
    eigenvalue_floor: float = _DEFAULT_EIGENVALUE_FLOOR,
    condition_limit: float = _DEFAULT_CONDITION_LIMIT,
) -> ProcessNoiseEstimate:
    """Estimate discrete process covariance from consecutive state samples.

    The model is ``x[k + 1] = F[k] x[k] + w[k]`` with zero-mean ``w``. The
    caller supplies the transition matrix or an ``(N - 1, d, d)`` stack and
    the timebase; the time values are checked for finite, strictly increasing
    order but no STP clock is inferred. Residuals are formed in one vectorized
    pass and their ``N - 1`` sample covariance is regularized. At least three
    residuals are required, or ``d + 1`` for a ``d``-dimensional state. This is
    local reference math, not a calibrated STP process model or evidence.
    """
    frame = _label(frame, "frame")
    timebase = _label(timebase, "timebase")
    loading = _finite_nonnegative(diagonal_loading, "diagonal_loading")
    floor = _finite_nonnegative(eigenvalue_floor, "eigenvalue_floor")
    limit = _condition_limit(condition_limit)

    times = _time_series(time_s)
    state_values = real_array(states, "states")
    if state_values.ndim == 1:
        state_samples = state_values.reshape(-1, 1)
    elif state_values.ndim == 2 and state_values.shape[1] >= 1:
        state_samples = state_values
    else:
        raise ValueError("states must have shape (N,) or (N, dimensions) with dimensions >= 1")
    sample_count = state_samples.shape[0]
    if times.size != sample_count:
        raise ValueError("time_s length must match the state sample count")

    dimension = state_samples.shape[1]
    transitions = real_array(transition_matrices, "transition_matrices")
    if transitions.ndim == 2:
        if transitions.shape != (dimension, dimension):
            raise ValueError("transition matrix shape must match the state dimension")
        predicted = state_samples[:-1] @ transitions.T
    elif transitions.ndim == 3:
        expected = (sample_count - 1, dimension, dimension)
        if transitions.shape != expected:
            raise ValueError(f"transition stack shape must be {expected}")
        predicted = np.einsum("nij,nj->ni", transitions, state_samples[:-1], optimize=True)
    else:
        raise ValueError("transition_matrices must have shape (d, d) or (N - 1, d, d)")

    transition_count = sample_count - 1
    minimum = max(_MIN_PROCESS_RESIDUALS, dimension + 1)
    if transition_count < minimum:
        raise ValueError(
            f"at least {minimum + 1} state samples are required for dimension "
            f"{dimension}; got {sample_count}"
        )

    residuals = state_samples[1:] - predicted
    if not np.isfinite(residuals).all():
        raise ValueError("state residuals are not finite")
    mean_residual = residuals.mean(axis=0)
    centered = residuals - mean_residual
    raw_covariance = centered.T @ centered / (transition_count - 1)
    if not np.isfinite(raw_covariance).all():
        raise ValueError("process covariance is not finite")
    regularization = regularize_covariance(
        raw_covariance,
        diagonal_loading=loading,
        eigenvalue_floor=floor,
        condition_limit=limit,
    )
    covariance = FrameCovariance(frame, timebase, regularization.matrix)
    return ProcessNoiseEstimate(
        covariance=covariance,
        sample_count=transition_count,
        mean_residual=mean_residual,
        regularization=regularization,
    )


def predict_covariance(
    prior: FrameCovariance,
    transition: npt.ArrayLike,
    process_noise: FrameCovariance,
    *,
    condition_limit: float = _DEFAULT_CONDITION_LIMIT,
) -> FrameCovariance:
    """Propagate covariance with ``F P F.T + Q``.

    ``prior`` and ``process_noise`` must carry the same frame and timebase.
    Both must be positive definite and within ``condition_limit``; singular or
    ill-conditioned inputs are rejected rather than silently adjusted. The
    transition may be singular and is only shape- and finiteness-checked. This
    is local reference math, not STP evidence.
    """
    prior_covariance = _require_frame_covariance(prior, "prior")
    process_covariance = _require_frame_covariance(process_noise, "process_noise")
    _require_same_metadata(prior_covariance, process_covariance, "prior", "process_noise")
    limit = _condition_limit(condition_limit)
    dimension = prior_covariance.matrix.shape[0]

    transition_matrix = real_array(transition, "transition")
    if transition_matrix.shape != (dimension, dimension):
        raise ValueError("transition shape must match the covariance dimension")
    prior_matrix = _positive_definite(prior_covariance.matrix, "prior covariance", limit)
    process_matrix = _positive_definite(
        process_covariance.matrix, "process noise covariance", limit
    )
    with np.errstate(over="ignore", invalid="ignore"):
        predicted = transition_matrix @ prior_matrix @ transition_matrix.T + process_matrix
    if not np.isfinite(predicted).all():
        raise ValueError("predicted covariance is not finite")
    predicted = _positive_definite(predicted, "predicted covariance", limit)
    return FrameCovariance(prior_covariance.frame, prior_covariance.timebase, predicted)


def update_covariance(
    prior: FrameCovariance,
    observation_matrix: npt.ArrayLike,
    measurement_covariance: FrameCovariance,
    *,
    condition_limit: float = _DEFAULT_CONDITION_LIMIT,
) -> CovarianceUpdate:
    """Update covariance with a linear observation and measurement noise.

    Computes ``S = H P H.T + R``, ``K = P H.T S^-1`` and the Joseph-form
    posterior ``(I - K H) P (I - K H).T + K R K.T``. The prior and measurement
    covariances must carry the same frame and timebase; the caller supplies
    ``H`` and is responsible for any physical frame mapping. Singular or
    ill-conditioned covariance inputs are rejected. This is local reference
    math, not STP evidence and not a state update.
    """
    prior_covariance = _require_frame_covariance(prior, "prior")
    measurement = _require_frame_covariance(measurement_covariance, "measurement_covariance")
    _require_same_metadata(prior_covariance, measurement, "prior", "measurement_covariance")
    limit = _condition_limit(condition_limit)
    state_dimension = prior_covariance.matrix.shape[0]
    measurement_dimension = measurement.matrix.shape[0]

    observation = real_array(observation_matrix, "observation_matrix")
    expected = (measurement_dimension, state_dimension)
    if observation.shape != expected:
        raise ValueError(f"observation matrix shape must be {expected}")
    prior_matrix = _positive_definite(prior_covariance.matrix, "prior covariance", limit)
    measurement_matrix = _positive_definite(measurement.matrix, "measurement covariance", limit)
    with np.errstate(over="ignore", invalid="ignore"):
        innovation = observation @ prior_matrix @ observation.T + measurement_matrix
    if not np.isfinite(innovation).all():
        raise ValueError("innovation covariance is not finite")
    innovation = _positive_definite(innovation, "innovation covariance", limit)
    try:
        gain = np.linalg.solve(innovation, observation @ prior_matrix).T
    except np.linalg.LinAlgError as error:
        raise ValueError("innovation covariance is singular") from error
    if not np.isfinite(gain).all():
        raise ValueError("Kalman gain is not finite")

    residual_map = np.eye(state_dimension) - gain @ observation
    with np.errstate(over="ignore", invalid="ignore"):
        posterior = (
            residual_map @ prior_matrix @ residual_map.T + gain @ measurement_matrix @ gain.T
        )
    if not np.isfinite(posterior).all():
        raise ValueError("posterior covariance is not finite")
    posterior = _positive_definite(posterior, "posterior covariance", limit)
    return CovarianceUpdate(
        posterior=FrameCovariance(prior_covariance.frame, prior_covariance.timebase, posterior),
        gain=gain,
        innovation_covariance=innovation,
    )
