# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Bijector base."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import contextlib

# Dependency imports
import numpy as np
import six
import tensorflow.compat.v2 as tf

from tensorflow_probability.python.internal import assert_util
from tensorflow_probability.python.internal import cache_util
from tensorflow_probability.python.internal import dtype_util
from tensorflow_probability.python.internal import name_util
from tensorflow_probability.python.internal import nest_util
from tensorflow_probability.python.internal import prefer_static as ps
from tensorflow_probability.python.math import gradient
from tensorflow.python.util import nest  # pylint: disable=g-direct-tensorflow-import


__all__ = [
    'Bijector',
]

JAX_MODE = False
SKIP_DTYPE_CHECKS = False

# Singleton object representing "no value", in cases where "None" is meaningful.
UNSPECIFIED = object()


class _BijectorMeta(abc.ABCMeta):
  """Helper metaclass for tfp.Bijector."""

  def __init__(cls, name, bases, dct):
    super(_BijectorMeta, cls).__init__(name, bases, dct)
    if not JAX_MODE:
      return
    def flatten(bij):
      param_names = set(bij._composite_tensor_nonshape_params)  # pylint: disable=protected-access
      components = {param_name: getattr(
          bij, param_name, value) for param_name, value
                    in bij.parameters.items() if param_name in param_names}
      metadata = {param_name: value for param_name, value
                  in bij.parameters.items() if param_name not in param_names}
      if components:
        keys, values = zip(*sorted(components.items()))
      else:
        keys, values = (), ()
      # Mimics the logic in `tfp.experimental.composite_tensor` where we
      # aggressively try to convert arguments into Tensors.
      def _maybe_convert_to_tensor(value, name):
        try:
          value = tf.convert_to_tensor(value, name=name)
        except (ValueError, TypeError, AssertionError):
          pass
        return value
      values = tuple([_maybe_convert_to_tensor(value, name) for value, name,
                      in zip(values, keys)])
      return values, (keys, metadata)
    def unflatten(info, xs):
      keys, metadata = info
      parameters = dict(list(zip(keys, xs)), **metadata)
      return cls(**parameters)
    from jax import tree_util  # pylint: disable=g-import-not-at-top
    tree_util.register_pytree_node(cls, flatten, unflatten)


@six.add_metaclass(_BijectorMeta)
class Bijector(tf.Module):
  r"""Interface for transformations of a `Distribution` sample.

  Bijectors can be used to represent any differentiable and injective
  (one to one) function defined on an open subset of `R^n`.  Some non-injective
  transformations are also supported (see 'Non Injective Transforms' below).

  #### Mathematical Details

  A `Bijector` implements a [smooth covering map](
  https://en.wikipedia.org/wiki/Local_diffeomorphism), i.e., a local
  diffeomorphism such that every point in the target has a neighborhood evenly
  covered by a map ([see also](
  https://en.wikipedia.org/wiki/Covering_space#Covering_of_a_manifold)).
  A `Bijector` is used by `TransformedDistribution` but can be generally used
  for transforming a `Distribution` generated `Tensor`. A `Bijector` is
  characterized by three operations:

  1. Forward

     Useful for turning one random outcome into another random outcome from a
     different distribution.

  2. Inverse

     Useful for 'reversing' a transformation to compute one probability in
     terms of another.

  3. `log_det_jacobian(x)`

     'The log of the absolute value of the determinant of the matrix of all
     first-order partial derivatives of the inverse function.'

     Useful for inverting a transformation to compute one probability in terms
     of another. Geometrically, the Jacobian determinant is the volume of the
     transformation and is used to scale the probability.

     We take the absolute value of the determinant before log to avoid NaN
     values.  Geometrically, a negative determinant corresponds to an
     orientation-reversing transformation.  It is ok for us to discard the sign
     of the determinant because we only integrate everywhere-nonnegative
     functions (probability densities) and the correct orientation is always the
     one that produces a nonnegative integrand.

  By convention, transformations of random variables are named in terms of the
  forward transformation. The forward transformation creates samples, the
  inverse is useful for computing probabilities.

  #### Example Uses

  - Basic properties:

  ```python
  x = ...  # A tensor.
  # Evaluate forward transformation.
  fwd_x = my_bijector.forward(x)
  x == my_bijector.inverse(fwd_x)
  x != my_bijector.forward(fwd_x)  # Not equal because x != g(g(x)).
  ```

  - Computing a log-likelihood:

  ```python
  def transformed_log_prob(bijector, log_prob, x):
    return (bijector.inverse_log_det_jacobian(x, event_ndims=0) +
            log_prob(bijector.inverse(x)))
  ```

  - Transforming a random outcome:

  ```python
  def transformed_sample(bijector, x):
    return bijector.forward(x)
  ```

  #### Example Bijectors

  - 'Exponential'

    ```none
    Y = g(X) = exp(X)
    X ~ Normal(0, 1)  # Univariate.
    ```

    Implies:

    ```none
      g^{-1}(Y) = log(Y)
      |Jacobian(g^{-1})(y)| = 1 / y
      Y ~ LogNormal(0, 1), i.e.,
      prob(Y=y) = |Jacobian(g^{-1})(y)| * prob(X=g^{-1}(y))
                = (1 / y) Normal(log(y); 0, 1)
    ```

    Here is an example of how one might implement the `Exp` bijector:

    ```python
      class Exp(Bijector):

        def __init__(self, validate_args=False, name='exp'):
          super(Exp, self).__init__(
              validate_args=validate_args,
              forward_min_event_ndims=0,
              name=name)

        def _forward(self, x):
          return tf.exp(x)

        def _inverse(self, y):
          return tf.log(y)

        def _inverse_log_det_jacobian(self, y):
          return -self._forward_log_det_jacobian(self._inverse(y))

        def _forward_log_det_jacobian(self, x):
          # Notice that we needn't do any reducing, even when`event_ndims > 0`.
          # The base Bijector class will handle reducing for us; it knows how
          # to do so because we called `super` `__init__` with
          # `forward_min_event_ndims = 0`.
          return x
      ```

  - 'Affine'

    ```none
    Y = g(X) = sqrtSigma * X + mu
    X ~ MultivariateNormal(0, I_d)
    ```

    Implies:

    ```none
      g^{-1}(Y) = inv(sqrtSigma) * (Y - mu)
      |Jacobian(g^{-1})(y)| = det(inv(sqrtSigma))
      Y ~ MultivariateNormal(mu, sqrtSigma) , i.e.,
      prob(Y=y) = |Jacobian(g^{-1})(y)| * prob(X=g^{-1}(y))
                = det(sqrtSigma)^(-d) *
                  MultivariateNormal(inv(sqrtSigma) * (y - mu); 0, I_d)
      ```

  #### Min_event_ndims and Naming

  Bijectors are named for the dimensionality of data they act on (i.e. without
  broadcasting). We can think of bijectors having an intrinsic `min_event_ndims`
  , which is the minimum number of dimensions for the bijector act on. For
  instance, a Cholesky decomposition requires a matrix, and hence
  `min_event_ndims=2`.

  Some examples:

  `AffineScalar:  min_event_ndims=0`
  `Affine:  min_event_ndims=1`
  `Cholesky:  min_event_ndims=2`
  `Exp:  min_event_ndims=0`
  `Sigmoid:  min_event_ndims=0`
  `SoftmaxCentered:  min_event_ndims=1`

  Note the difference between `Affine` and `AffineScalar`. `AffineScalar`
  operates on scalar events, whereas `Affine` operates on vector-valued events.

  More generally, there is a `forward_min_event_ndims` and an
  `inverse_min_event_ndims`. In most cases, these will be the same.
  However, for some shape changing bijectors, these will be different
  (e.g. a bijector which pads an extra dimension at the end, might have
  `forward_min_event_ndims=0` and `inverse_min_event_ndims=1`.

  ##### Additional Considerations for "Multi Tensor" Bijectors

  Bijectors which operate on structures of `Tensor` require structured
  `min_event_ndims` matching the structure of the inputs. In these cases,
  `min_event_ndims` describes both the minimum dimensionality *and* the
  structure of arguments to `forward` and `inverse`. For example:

  ```
  Split([sizes], axis):
    forward_min_event_ndims=-axis
    inverse_min_event_ndims=[-axis] * len(sizes)
  ```

  Note: By default, we require `shape(x[i])[-event_ndims:-min_event_ndims]` to
  be identical for all elements `i` of the structured input `x`. Specifically,
  broadcasting over non-minimal event-dims is not allowed for structured inputs.
  In cases where broadcasting is used as a "computational shorthand" for a dense
  operation (that is, the _broadcasted_ inputs are assumed to be independent),
  users should set `bijector._allow_event_shape_broadcasting = True`.

  Finally, some bijectors that operate on structures of inputs may not know
  the minimum structured rank of their inputs without calltime shape information
  (Composite bijectors, for example). In these cases, both `min_event_ndims`
  properties will indicate the expected *structure* of inputs and outputs,
  but the component values may be `None`.

  #### Jacobian Determinant

  The Jacobian determinant is a reduction over `event_ndims - min_event_ndims`
  (`forward_min_event_ndims` for `forward_log_det_jacobian` and
  `inverse_min_event_ndims` for `inverse_log_det_jacobian`).
  To see this, consider the `Exp` `Bijector` applied to a `Tensor` which has
  sample, batch, and event (S, B, E) shape semantics. Suppose the `Tensor`'s
  partitioned-shape is `(S=[4], B=[2], E=[3, 3])`. The shape of the `Tensor`
  returned by `forward` and `inverse` is unchanged, i.e., `[4, 2, 3, 3]`.
  However the shape returned by `inverse_log_det_jacobian` is `[4, 2]` because
  the Jacobian determinant is a reduction over the event dimensions.

  Another example is the `Affine` `Bijector`. Because `min_event_ndims = 1`, the
  Jacobian determinant reduction is over `event_ndims - 1`.

  It is sometimes useful to implement the inverse Jacobian determinant as the
  negative forward Jacobian determinant. For example,

  ```python
  def _inverse_log_det_jacobian(self, y):
     return -self._forward_log_det_jac(self._inverse(y))  # Note negation.
  ```

  The correctness of this approach can be seen from the following claim.

  - Claim:

      Assume `Y = g(X)` is a bijection whose derivative exists and is nonzero
      for its domain, i.e., `dY/dX = d/dX g(X) != 0`. Then:

      ```none
      (log o det o jacobian o g^{-1})(Y) = -(log o det o jacobian o g)(X)
      ```

  - Proof:

      From the bijective, nonzero differentiability of `g`, the
      [inverse function theorem](
          https://en.wikipedia.org/wiki/Inverse_function_theorem)
      implies `g^{-1}` is differentiable in the image of `g`.
      Applying the chain rule to `y = g(x) = g(g^{-1}(y))` yields
      `I = g'(g^{-1}(y))*g^{-1}'(y)`.
      The same theorem also implies `g^{-1}'` is non-singular therefore:
      `inv[ g'(g^{-1}(y)) ] = g^{-1}'(y)`.
      The claim follows from [properties of determinant](
  https://en.wikipedia.org/wiki/Determinant#Multiplicativity_and_matrix_groups).

  Generally its preferable to directly implement the inverse Jacobian
  determinant.  This should have superior numerical stability and will often
  share subgraphs with the `_inverse` implementation.

  #### Is_constant_jacobian

  Certain bijectors will have constant jacobian matrices. For instance, the
  `Affine` bijector encodes multiplication by a matrix plus a shift, with
  jacobian matrix, the same aforementioned matrix.

  `is_constant_jacobian` encodes the fact that the jacobian matrix is constant.
  The semantics of this argument are the following:

    * Repeated calls to 'log_det_jacobian' functions with the same
      `event_ndims` (but not necessarily same input), will return the first
      computed jacobian (because the matrix is constant, and hence is input
      independent).
    * `log_det_jacobian` implementations are merely broadcastable to the true
      `log_det_jacobian` (because, again, the jacobian matrix is input
      independent). Specifically, `log_det_jacobian` is implemented as the
      log jacobian determinant for a single input.

      ```python
      class Identity(Bijector):

        def __init__(self, validate_args=False, name='identity'):
          super(Identity, self).__init__(
              is_constant_jacobian=True,
              validate_args=validate_args,
              forward_min_event_ndims=0,
              name=name)

        def _forward(self, x):
          return x

        def _inverse(self, y):
          return y

        def _inverse_log_det_jacobian(self, y):
          return -self._forward_log_det_jacobian(self._inverse(y))

        def _forward_log_det_jacobian(self, x):
          # The full log jacobian determinant would be tf.zero_like(x).
          # However, we circumvent materializing that, since the jacobian
          # calculation is input independent, and we specify it for one input.
          return tf.constant(0., x.dtype)

      ```

  #### Subclass Requirements

  - Subclasses typically implement:

      - `_forward`,
      - `_inverse`,
      - `_inverse_log_det_jacobian`,
      - `_forward_log_det_jacobian` (optional),
      - `_is_increasing` (scalar bijectors only)

    The `_forward_log_det_jacobian` is called when the bijector is inverted via
    the `Invert` bijector. If undefined, a slightly less efficiently
    calculation, `-1 * _inverse_log_det_jacobian`, is used.

    If the bijector changes the shape of the input, you must also implement:

      - _forward_event_shape_tensor,
      - _forward_event_shape (optional),
      - _inverse_event_shape_tensor,
      - _inverse_event_shape (optional).

    By default the event-shape is assumed unchanged from input.

    Multipart bijectors, which operate on structures of tensors, may implement
    additional methods to propogate calltime dtype information over any changes
    to structure. These methods are:

      - _forward_dtype
      - _inverse_dtype

  - If the `Bijector`'s use is limited to `TransformedDistribution` (or friends
    like `QuantizedDistribution`) then depending on your use, you may not need
    to implement all of `_forward` and `_inverse` functions.

    Examples:

      1. Sampling (e.g., `sample`) only requires `_forward`.
      2. Probability functions (e.g., `prob`, `cdf`, `survival`) only require
         `_inverse` (and related).
      3. Only calling probability functions on the output of `sample` means
        `_inverse` can be implemented as a cache lookup.

    See 'Example Uses' [above] which shows how these functions are used to
    transform a distribution. (Note: `_forward` could theoretically be
    implemented as a cache lookup but this would require controlling the
    underlying sample generation mechanism.)

  #### Non Injective Transforms

  **WARNING** Handling of non-injective transforms is subject to change.

  Non injective maps `g` are supported, provided their domain `D` can be
  partitioned into `k` disjoint subsets, `Union{D1, ..., Dk}`, such that,
  ignoring sets of measure zero, the restriction of `g` to each subset is a
  differentiable bijection onto `g(D)`.  In particular, this implies that for
  `y in g(D)`, the set inverse, i.e. `g^{-1}(y) = {x in D : g(x) = y}`, always
  contains exactly `k` distinct points.

  The property, `_is_injective` is set to `False` to indicate that the bijector
  is not injective, yet satisfies the above condition.

  The usual bijector API is modified in the case `_is_injective is False` (see
  method docstrings for specifics).  Here we show by example the `AbsoluteValue`
  bijector.  In this case, the domain `D = (-inf, inf)`, can be partitioned
  into `D1 = (-inf, 0)`, `D2 = {0}`, and `D3 = (0, inf)`.  Let `gi` be the
  restriction of `g` to `Di`, then both `g1` and `g3` are bijections onto
  `(0, inf)`, with `g1^{-1}(y) = -y`, and `g3^{-1}(y) = y`.  We will use
  `g1` and `g3` to define bijector methods over `D1` and `D3`.  `D2 = {0}` is
  an oddball in that `g2` is one to one, and the derivative is not well defined.
  Fortunately, when considering transformations of probability densities
  (e.g. in `TransformedDistribution`), sets of measure zero have no effect in
  theory, and only a small effect in 32 or 64 bit precision.  For that reason,
  we define `inverse(0)` and `inverse_log_det_jacobian(0)` both as `[0, 0]`,
  which is convenient and results in a left-semicontinuous pdf.


  ```python
  abs = tfp.bijectors.AbsoluteValue()

  abs.forward(-1.)
  ==> 1.

  abs.forward(1.)
  ==> 1.

  abs.inverse(1.)
  ==> (-1., 1.)

  # The |dX/dY| is constant, == 1.  So Log|dX/dY| == 0.
  abs.inverse_log_det_jacobian(1., event_ndims=0)
  ==> (0., 0.)

  # Special case handling of 0.
  abs.inverse(0.)
  ==> (0., 0.)

  abs.inverse_log_det_jacobian(0., event_ndims=0)
  ==> (0., 0.)
  ```

  """

  _TF_MODULE_IGNORED_PROPERTIES = tf.Module._TF_MODULE_IGNORED_PROPERTIES.union(
      (
          '_graph_parents',
          '_is_constant_jacobian',
          '_cache',
          '_forward_min_event_ndims',
          '_inverse_min_event_ndims',
      ))

  _cache = cache_util.BijectorCache()

  @abc.abstractmethod
  def __init__(self,
               graph_parents=None,
               is_constant_jacobian=False,
               validate_args=False,
               dtype=None,
               forward_min_event_ndims=UNSPECIFIED,
               inverse_min_event_ndims=UNSPECIFIED,
               parameters=None,
               name=None):
    """Constructs Bijector.

    A `Bijector` transforms random variables into new random variables.

    Examples:

    ```python
    # Create the Y = g(X) = X transform.
    identity = Identity()

    # Create the Y = g(X) = exp(X) transform.
    exp = Exp()
    ```

    See `Bijector` subclass docstring for more details and specific examples.

    Args:
      graph_parents: Python list of graph prerequisites of this `Bijector`.
      is_constant_jacobian: Python `bool` indicating that the Jacobian matrix is
        not a function of the input.
      validate_args: Python `bool`, default `False`. Whether to validate input
        with asserts. If `validate_args` is `False`, and the inputs are invalid,
        correct behavior is not guaranteed.
      dtype: `tf.dtype` supported by this `Bijector`. `None` means dtype is not
        enforced. For multipart bijectors, this value is expected to be the
        same for all elements of the input and output structures.
      forward_min_event_ndims: Python `integer` (structure) indicating the
        minimum number of dimensions on which `forward` operates.
      inverse_min_event_ndims: Python `integer` (structure) indicating the
        minimum number of dimensions on which `inverse` operates. Will be set to
        `forward_min_event_ndims` by default, if no value is provided.
      parameters: Python `dict` of parameters used to instantiate this
        `Bijector`. Bijector instances with identical types, names, and
        `parameters` share an input/output cache. `parameters` dicts are
        keyed by strings and are identical if their keys are identical and if
        corresponding values have identical hashes (or object ids, for
        unhashable objects).
      name: The name to give Ops created by the initializer.

    Raises:
      ValueError:  If neither `forward_min_event_ndims` and
        `inverse_min_event_ndims` are specified, or if either of them is
        negative.
      ValueError:  If a member of `graph_parents` is not a `Tensor`.
    """
    if not name:
      name = type(self).__name__
      name = name_util.camel_to_lower_snake(name)
    name = name_util.get_name_scope_name(name)
    name = name_util.strip_invalid_chars(name)
    super(Bijector, self).__init__(name=name)
    self._name = name
    # TODO(b/176242804): Infer `parameters` if not specified by the child class.
    self._parameters = self._no_dependency(parameters)

    self._graph_parents = self._no_dependency(graph_parents or [])

    self._is_constant_jacobian = is_constant_jacobian
    self._validate_args = validate_args
    self._dtype = dtype

    self._initial_parameter_control_dependencies = tuple(
        d for d in self._parameter_control_dependencies(is_init=True)
        if d is not None)
    if self._initial_parameter_control_dependencies:
      self._initial_parameter_control_dependencies = (
          tf.group(*self._initial_parameter_control_dependencies),)

    # Validate min_event_ndims, if all values are known.
    # Note that bijectors without known min_event_ndims (eg, Composite)
    # must override `_call_{ldj_func}` instead of `_{ldj_func}`.
    if (forward_min_event_ndims is UNSPECIFIED
        and inverse_min_event_ndims is UNSPECIFIED):
      raise ValueError('Must specify at least one of `forward_min_event_ndims` '
                       'and `inverse_min_event_ndims`.')
    elif forward_min_event_ndims is UNSPECIFIED:
      forward_min_event_ndims = inverse_min_event_ndims
    elif inverse_min_event_ndims is UNSPECIFIED:
      inverse_min_event_ndims = forward_min_event_ndims

    # Prevent tf.Module from wrapping structured min_event_ndims in proxies.
    # We use (forward|inverse)_min_event_ndims to specify input/output
    # structures, so it is important that we retain the original containers.
    self._forward_min_event_ndims = self._no_dependency(forward_min_event_ndims)
    self._inverse_min_event_ndims = self._no_dependency(inverse_min_event_ndims)
    self._has_static_min_event_ndims = None not in (
        nest.flatten([forward_min_event_ndims, inverse_min_event_ndims]))

    # Whether to allow broadcasting over the (non-minimal) event-shape for
    # structured inputs. When `False` (default), assert that LDJ reduction
    # shapes are identical for all components of nested inputs. When `True`,
    # event-shape broadcasing is allow, but LDJ may be incorrect.
    self._allow_event_shape_broadcasting = False

    # Batch shape implied by the bijector's parameters, for use in validating
    # LDJ shapes (currently only used in multipart bijectors.)
    self._parameter_batch_shape = None

    for i, t in enumerate(self._graph_parents):
      if t is None or not tf.is_tensor(t):
        raise ValueError('Graph parent item %d is not a Tensor; %s.' % (i, t))

  @property
  def graph_parents(self):
    """Returns this `Bijector`'s graph_parents as a Python list."""
    return self._graph_parents

  @property
  def forward_min_event_ndims(self):
    """Returns the minimal number of dimensions bijector.forward operates on.

    Multipart bijectors return structured `ndims`, which indicates the
    expected structure of their inputs. Some multipart bijectors, notably
    Composites, may return structures of `None`.
    """
    return self._forward_min_event_ndims

  @property
  def inverse_min_event_ndims(self):
    """Returns the minimal number of dimensions bijector.inverse operates on.

    Multipart bijectors return structured `event_ndims`, which indicates the
    expected structure of their outputs. Some multipart bijectors, notably
    Composites, may return structures of `None`.
    """
    return self._inverse_min_event_ndims

  @property
  def has_static_min_event_ndims(self):
    """Returns True if the bijector has statically-known `min_event_ndims`."""
    return self._has_static_min_event_ndims

  @property
  def is_constant_jacobian(self):
    """Returns true iff the Jacobian matrix is not a function of x.

    Note: Jacobian matrix is either constant for both forward and inverse or
    neither.

    Returns:
      is_constant_jacobian: Python `bool`.
    """
    return self._is_constant_jacobian

  @property
  def _is_injective(self):
    """Returns true iff the forward map `g` is injective (one-to-one function).

    **WARNING** This hidden property and its behavior are subject to change.

    Note:  Non-injective maps `g` are supported, provided their domain `D` can
    be partitioned into `k` disjoint subsets, `Union{D1, ..., Dk}`, such that,
    ignoring sets of measure zero, the restriction of `g` to each subset is a
    differentiable bijection onto `g(D)`.

    Returns:
      is_injective: Python `bool`.
    """
    return True

  @property
  def _is_scalar(self):
    return (tf.get_static_value(self._forward_min_event_ndims) == 0 and
            tf.get_static_value(self._inverse_min_event_ndims) == 0)

  @property
  def _is_permutation(self):
    """Whether `y` is purely a reordering / restructuring of `x`."""
    return False

  @property
  def validate_args(self):
    """Returns True if Tensor arguments will be validated."""
    return self._validate_args

  @property
  def dtype(self):
    return self._dtype

  @property
  def name(self):
    """Returns the string name of this `Bijector`."""
    return self._name

  @property
  def parameters(self):
    """Dictionary of parameters used to instantiate this `Bijector`."""
    # Remove "self", "__class__", or other special variables. These can appear
    # if the subclass used:
    # `parameters = dict(locals())`.
    if self._parameters is None:
      return None
    return {k: v for k, v in self._parameters.items()
            if not k.startswith('__') and k != 'self'}

  def __hash__(self):
    return hash(cache_util.hashable_structure((
        type(self), self._get_parameterization())))

  def __eq__(self, other):
    if type(self) is not type(other):
      return False

    try:
      tf.nest.assert_same_structure(self._get_parameterization(),
                                    other._get_parameterization())
    except (ValueError, TypeError):
      return False

    self_params = tf.nest.flatten(self._get_parameterization())
    other_params = tf.nest.flatten(other._get_parameterization())

    for (p1, p2) in zip(self_params, other_params):
      if p1 is p2:
        continue
      if tf.is_tensor(p1):
        p1 = tf.get_static_value(p1)
      if tf.is_tensor(p2):
        p2 = tf.get_static_value(p2)
      p1_isarray = getattr(p1, '__array__', None) is not None
      p2_isarray = getattr(p2, '__array__', None) is not None
      if p1_isarray != p2_isarray:
        return False
      if p1_isarray and p2_isarray:
        if p1.shape != p2.shape:
          return False
        if not np.all(np.equal(p1, p2)):
          return False
      if p1 != p2:
        return False
    return True

  def _get_parameterization(self):
    if self.parameters is None:
      # If a user-written bijector doesn't specify `parameters`, we must assume
      # that all instances are unique.
      # TODO(b/176242804): this can be removed if we always infer `parameters`.
      return id(self)
    return self.parameters

  def __call__(self, value, name=None, **kwargs):
    """Applies or composes the `Bijector`, depending on input type.

    This is a convenience function which applies the `Bijector` instance in
    three different ways, depending on the input:

    1. If the input is a `tfd.Distribution` instance, return
       `tfd.TransformedDistribution(distribution=input, bijector=self)`.
    2. If the input is a `tfb.Bijector` instance, return
       `tfb.Chain([self, input])`.
    3. Otherwise, return `self.forward(input)`

    Args:
      value: A `tfd.Distribution`, `tfb.Bijector`, or a (structure of) `Tensor`.
      name: Python `str` name given to ops created by this function.
      **kwargs: Additional keyword arguments passed into the created
        `tfd.TransformedDistribution`, `tfb.Bijector`, or `self.forward`.

    Returns:
      composition: A `tfd.TransformedDistribution` if the input was a
        `tfd.Distribution`, a `tfb.Chain` if the input was a `tfb.Bijector`, or
        a (structure of) `Tensor` computed by `self.forward`.

    #### Examples

    ```python
    sigmoid = tfb.Reciprocal()(
        tfb.Shift(shift=1.)(
          tfb.Exp()(
            tfb.Scale(scale=-1.))))
    # ==> `tfb.Chain([
    #         tfb.Reciprocal(),
    #         tfb.Shift(shift=1.),
    #         tfb.Exp(),
    #         tfb.Scale(scale=-1.),
    #      ])`  # ie, `tfb.Sigmoid()`

    log_normal = tfb.Exp()(tfd.Normal(0, 1))
    # ==> `tfd.TransformedDistribution(tfd.Normal(0, 1), tfb.Exp())`

    tfb.Exp()([-1., 0., 1.])
    # ==> tf.exp([-1., 0., 1.])
    ```

    """

    # To avoid circular dependencies and keep the implementation local to the
    # `Bijector` class, we violate PEP8 guidelines and import here rather than
    # at the top of the file.
    from tensorflow_probability.python.bijectors import chain  # pylint: disable=g-import-not-at-top
    from tensorflow_probability.python.distributions import distribution  # pylint: disable=g-import-not-at-top
    from tensorflow_probability.python.distributions import transformed_distribution  # pylint: disable=g-import-not-at-top

    # TODO(b/128841942): Handle Conditional distributions and bijectors.
    if type(value) is transformed_distribution.TransformedDistribution:  # pylint: disable=unidiomatic-typecheck
      # We cannot accept subclasses with different constructors here, because
      # subclass constructors may accept constructor arguments TD doesn't know
      # how to handle. e.g. `TypeError: __init__() got an unexpected keyword
      # argument 'allow_nan_stats'` when doing
      # `tfb.Identity()(tfd.Chi(df=1., allow_nan_stats=True))`.
      new_kwargs = value.parameters
      new_kwargs.update(kwargs)
      new_kwargs['name'] = name or new_kwargs.get('name', None)
      new_kwargs['bijector'] = self(value.bijector)
      return transformed_distribution.TransformedDistribution(**new_kwargs)

    if isinstance(value, distribution.Distribution):
      return transformed_distribution.TransformedDistribution(
          distribution=value,
          bijector=self,
          name=name,
          **kwargs)

    if isinstance(value, chain.Chain):
      new_kwargs = kwargs.copy()
      new_kwargs['bijectors'] = [self] + ([] if value.bijectors is None
                                          else list(value.bijectors))
      if 'validate_args' not in new_kwargs:
        new_kwargs['validate_args'] = value.validate_args
      new_kwargs['name'] = name or value.name
      return chain.Chain(**new_kwargs)

    if isinstance(value, Bijector):
      return chain.Chain([self, value], name=name, **kwargs)

    return self.forward(value, name=name or 'forward', **kwargs)

  def _forward_event_shape_tensor(self, input_shape):
    """Subclass implementation for `forward_event_shape_tensor` function."""
    # By default, we assume event_shape is unchanged.
    return input_shape

  def forward_event_shape_tensor(self,
                                 input_shape,
                                 name='forward_event_shape_tensor'):
    """Shape of a single sample from a single batch as an `int32` 1D `Tensor`.

    Args:
      input_shape: `Tensor`, `int32` vector (structure) indicating event-portion
        shape passed into `forward` function.
      name: name to give to the op

    Returns:
      forward_event_shape_tensor: `Tensor`, `int32` vector (structure)
        indicating event-portion shape after applying `forward`.
    """
    with self._name_and_control_scope(name):
      # Use statically-known structure from min_event_ndims.
      input_shape_dtype = nest_util.broadcast_structure(
          self.forward_min_event_ndims, tf.int32)
      input_shape = nest_util.convert_to_nested_tensor(
          input_shape, dtype_hint=input_shape_dtype,
          name='input_event_shape', allow_packing=True)
      # Wrap inputs in identity to make sure control_scope is respected.
      input_shape = nest.map_structure(tf.identity, input_shape)

      # Refer to static-dtype to get structure; we don't care about ntype here.
      output_shape_dtype = nest_util.broadcast_structure(
          self.inverse_min_event_ndims, tf.int32)
      return nest_util.convert_to_nested_tensor(
          self._forward_event_shape_tensor(input_shape),
          dtype_hint=output_shape_dtype,
          name='output_event_shape', allow_packing=True)

  def _forward_event_shape(self, input_shape):
    """Subclass implementation for `forward_event_shape` public function."""
    # By default, we assume event_shape is unchanged.
    return input_shape

  def forward_event_shape(self, input_shape):
    """Shape of a single sample from a single batch as a `TensorShape`.

    Same meaning as `forward_event_shape_tensor`. May be only partially defined.

    Args:
      input_shape: `TensorShape` (structure) indicating event-portion shape
        passed into `forward` function.

    Returns:
      forward_event_shape_tensor: `TensorShape` (structure) indicating
        event-portion shape after applying `forward`. Possibly unknown.
    """
    # Use statically-known dtype attribute to infer structure.
    input_shape = nest.map_structure_up_to(
        self.forward_min_event_ndims, tf.TensorShape,
        nest_util.coerce_structure(self.forward_min_event_ndims, input_shape),
        check_types=False)
    return nest.map_structure_up_to(
        self.inverse_min_event_ndims, tf.TensorShape,
        self._forward_event_shape(input_shape))

  def _inverse_event_shape_tensor(self, output_shape):
    """Subclass implementation for `inverse_event_shape_tensor` function."""
    # By default, we assume event_shape is unchanged.
    return output_shape

  def inverse_event_shape_tensor(self,
                                 output_shape,
                                 name='inverse_event_shape_tensor'):
    """Shape of a single sample from a single batch as an `int32` 1D `Tensor`.

    Args:
      output_shape: `Tensor`, `int32` vector (structure) indicating
        event-portion shape passed into `inverse` function.
      name: name to give to the op

    Returns:
      inverse_event_shape_tensor: `Tensor`, `int32` vector (structure)
        indicating event-portion shape after applying `inverse`.
    """
    with self._name_and_control_scope(name):
      output_shape = nest_util.convert_to_nested_tensor(
          output_shape, name='output_event_shape',
          dtype_hint=nest_util.broadcast_structure(
              self.inverse_min_event_ndims, tf.int32),
          allow_packing=True)
      # Wrap inputs in identity to make sure control_scope is respected.
      output_shape = nest.map_structure(tf.identity, output_shape)

      return nest_util.convert_to_nested_tensor(
          self._inverse_event_shape_tensor(output_shape),
          name='input_event_shape',
          dtype_hint=nest_util.broadcast_structure(
              self.forward_min_event_ndims, tf.int32),
          allow_packing=True)

  def _inverse_event_shape(self, output_shape):
    """Subclass implementation for `inverse_event_shape` public function."""
    # By default, we assume event_shape is unchanged.
    return output_shape

  def inverse_event_shape(self, output_shape):
    """Shape of a single sample from a single batch as a `TensorShape`.

    Same meaning as `inverse_event_shape_tensor`. May be only partially defined.

    Args:
      output_shape: `TensorShape` (structure) indicating event-portion shape
        passed into `inverse` function.

    Returns:
      inverse_event_shape_tensor: `TensorShape` (structure) indicating
        event-portion shape after applying `inverse`. Possibly unknown.
    """
    # Use statically-known dtype attribute to infer structure.
    output_shape = nest.map_structure_up_to(
        self.inverse_min_event_ndims, tf.TensorShape,
        nest_util.coerce_structure(self.inverse_min_event_ndims, output_shape),
        check_types=False)
    return nest.map_structure_up_to(
        self.forward_min_event_ndims, tf.TensorShape,
        self._inverse_event_shape(output_shape))

  @classmethod
  def _parameter_properties(cls, dtype):
    raise NotImplementedError(
        '_parameter_properties` is not implemented: {}'.format(cls.__name__))

  @classmethod
  def parameter_properties(cls, dtype=tf.float32):
    """Returns a dict mapping constructor arg names to property annotations.

    This dict should include an entry for each of the bijector's
    `Tensor`-valued constructor arguments.

    Args:
      dtype: Optional float `dtype` to assume for continuous-valued parameters.
        Some constraining bijectors require advance knowledge of the dtype
        because certain constants (e.g., `tfb.Softplus.low`) must be
        instantiated with the same dtype as the values to be transformed.
    Returns:
      parameter_properties: A
        `str -> `tfp.python.internal.parameter_properties.ParameterProperties`
        dict mapping constructor argument names to `ParameterProperties`
        instances.
    """
    with tf.name_scope('parameter_properties'):
      return cls._parameter_properties(dtype)

  @classmethod
  def _params_event_ndims(cls):
    """Returns a dict mapping constructor argument names to per-event rank.

    The ranks are pulled from `cls.parameter_properties()`; this is a
    convenience wrapper.

    Returns:
      params_event_ndims: Per-event parameter ranks, a `str->int dict`.
    """
    return {
        param_name: param.event_ndims
        for param_name, param in cls.parameter_properties().items()
    }

  def _forward(self, x):
    """Subclass implementation for `forward` public function."""
    raise NotImplementedError('forward not implemented.')

  def _call_forward(self, x, name, **kwargs):
    """Wraps call to _forward, allowing extra shared logic."""
    with self._name_and_control_scope(name):
      dtype = self.inverse_dtype(**kwargs)
      x = nest_util.convert_to_nested_tensor(
          x, name='x', dtype_hint=dtype,
          dtype=None if SKIP_DTYPE_CHECKS else dtype,
          allow_packing=True)
      if not self._is_injective:  # No caching for non-injective
        return self._forward(x, **kwargs)
      return self._cache.forward(x, **kwargs)

  def forward(self, x, name='forward', **kwargs):
    """Returns the forward `Bijector` evaluation, i.e., X = g(Y).

    Args:
      x: `Tensor` (structure). The input to the 'forward' evaluation.
      name: The name to give this op.
      **kwargs: Named arguments forwarded to subclass implementation.

    Returns:
      `Tensor` (structure).

    Raises:
      TypeError: if `self.dtype` is specified and `x.dtype` is not
        `self.dtype`.
      NotImplementedError: if `_forward` is not implemented.
    """
    return self._call_forward(x, name, **kwargs)

  @classmethod
  def _is_increasing(cls, **kwargs):
    """Subclass implementation for `is_increasing` public function."""
    raise NotImplementedError('`_is_increasing` not implemented.')

  def _call_is_increasing(self, name, **kwargs):
    """Wraps call to _is_increasing, allowing extra shared logic."""
    with self._name_and_control_scope(name):
      return tf.identity(self._is_increasing(**kwargs))

  def _internal_is_increasing(self, name='is_increasing', **kwargs):
    """For scalar bijectors, returns True where `d forward(x) / d x > 0`.

    This method, like `_is_injective`, is part of a contract with
    `TransformedDistribution`. This method supports the correctness of scalar
    `quantile` / `cdf` / `survival_function` for transformed distributions.

    Args:
      name: The name to give this op.
      **kwargs: Named arguments forwarded to subclass implementation.

    Returns:
      A python `bool` or a `tf.bool` `Tensor`.
    """
    return self._call_is_increasing(name, **kwargs)

  def _inverse(self, y):
    """Subclass implementation for `inverse` public function."""
    raise NotImplementedError('inverse not implemented')

  def _call_inverse(self, y, name, **kwargs):
    """Wraps call to _inverse, allowing extra shared logic."""
    with self._name_and_control_scope(name):
      dtype = self.forward_dtype(**kwargs)
      y = nest_util.convert_to_nested_tensor(
          y, name='y', dtype_hint=dtype,
          dtype=None if SKIP_DTYPE_CHECKS else dtype,
          allow_packing=True)

      if not self._is_injective:  # No caching for non-injective
        return self._inverse(y, **kwargs)
      return self._cache.inverse(y, **kwargs)

  def inverse(self, y, name='inverse', **kwargs):
    """Returns the inverse `Bijector` evaluation, i.e., X = g^{-1}(Y).

    Args:
      y: `Tensor` (structure). The input to the 'inverse' evaluation.
      name: The name to give this op.
      **kwargs: Named arguments forwarded to subclass implementation.

    Returns:
      `Tensor` (structure), if this bijector is injective.
        If not injective, returns the k-tuple containing the unique
        `k` points `(x1, ..., xk)` such that `g(xi) = y`.

    Raises:
      TypeError: if `y`'s structured dtype is incompatible with the expected
        output dtype.
      NotImplementedError: if `_inverse` is not implemented.
    """
    return self._call_inverse(y, name, **kwargs)

  def _call_inverse_log_det_jacobian(self, y, event_ndims, name, **kwargs):
    """Wraps call to _inverse_log_det_jacobian, allowing extra shared logic.

    Specifically, this method
      - adds a name scope,
      - performs validations,
      - handles the special case of non-injective Bijector (skip caching and
        reduce_sum over the values at the multiple points in the preimage of `y`
        under the non-injective transformation)

    so that sub-classes don't have to worry about this stuff.

    Args:
      y: same as in `inverse_log_det_jacobian`
      event_ndims: same as in `inverse_log_det_jacobian`
      name: same as in `inverse_log_det_jacobian`
      **kwargs: same as in `inverse_log_det_jacobian`

    Returns:
      ildj: the inverse log det jacobian at `y`. Also updates the cache as
        needed.
    """
    if not self.has_static_min_event_ndims:
      raise NotImplementedError(
          'Subclasses without static `inverse_min_event_ndims` must override '
          '`_call_inverse_log_det_jacobian`.')

    with self._name_and_control_scope(name):
      dtype = self.forward_dtype(**kwargs)
      y = nest_util.convert_to_nested_tensor(
          y, name='y', dtype_hint=dtype,
          dtype=None if SKIP_DTYPE_CHECKS else dtype,
          allow_packing=True)

      if event_ndims is None:
        event_ndims = self.forward_min_event_ndims

      reduce_shape, assertions = ldj_reduction_shape(
          nest.map_structure(ps.shape, y),
          event_ndims=nest_util.coerce_structure(
              self.inverse_min_event_ndims, event_ndims),
          min_event_ndims=self._inverse_min_event_ndims,
          parameter_batch_shape=self._parameter_batch_shape,
          allow_event_shape_broadcasting=self._allow_event_shape_broadcasting,
          validate_args=self.validate_args)

      # Make sure we have validated reduce_shape before continuing on.
      with tf.control_dependencies(assertions):
        if not self._is_injective:
          # Non-injective bijectors don't use caching, and the resulting
          # LDJ is a tuple of LDJ over possible partitions on `x`.
          return tuple(
              self._reduce_jacobian_det_over_shape(ildj, reduce_shape)
              for ildj in self._inverse_log_det_jacobian(y, **kwargs))

        # Make sure the unreduced ILDJ is in the cache.
        attrs = self._cache.inverse_attributes(y, **kwargs)
        if 'ildj' in attrs:
          ildj = attrs['ildj']
        elif hasattr(self, '_inverse_log_det_jacobian'):
          ildj = attrs['ildj'] = self._inverse_log_det_jacobian(y, **kwargs)
        elif hasattr(self, '_forward_log_det_jacobian'):
          x = self.inverse(y, **kwargs)  # Fall back to computing `-fldj(x)`
          ildj = attrs['ildj'] = -self._forward_log_det_jacobian(x, **kwargs)
        elif self._is_scalar:
          ildj = _autodiff_log_det_jacobian(self._inverse, y)
        else:
          raise NotImplementedError(
              'Neither _forward_log_det_jacobian nor _inverse_log_det_jacobian '
              'is implemented. One or the other is required.')

        return self._reduce_jacobian_det_over_shape(ildj, reduce_shape)

  def inverse_log_det_jacobian(self,
                               y,
                               event_ndims=None,
                               name='inverse_log_det_jacobian',
                               **kwargs):
    """Returns the (log o det o Jacobian o inverse)(y).

    Mathematically, returns: `log(det(dX/dY))(Y)`. (Recall that: `X=g^{-1}(Y)`.)

    Note that `forward_log_det_jacobian` is the negative of this function,
    evaluated at `g^{-1}(y)`.

    Args:
      y: `Tensor` (structure). The input to the 'inverse' Jacobian determinant
        evaluation.
      event_ndims: Optional number of dimensions in the probabilistic events
        being transformed; this must be greater than or equal to
        `self.inverse_min_event_ndims`. If `event_ndims` is specified, the
        log Jacobian determinant is summed over its final
        `event_ndims - self.inverse_min_event_ndims` dimensions to produce a
        scalar log-determinant for each event. Otherwise
        (if `event_ndims` is `None`), no reduction is performed.
        Multipart bijectors require *structured* event_ndims, such that
        `rank(y[i]) - rank(event_ndims[i])` is the same for all elements `i` of
        the structured input. Furthermore, the first `event_ndims[i]` of each
        `x[i].shape` must be the same for all `i` (broadcasting is not allowed).
        Multipart bijectors for which `self.has_static_min_event_ndims == False`
        require `event_ndims` to be passed explicitly, and will raise a
        `ValueError` if `event_ndims` is `None`.
        Default value: `None` (equivalent to `self.inverse_min_event_ndims`).
      name: The name to give this op.
      **kwargs: Named arguments forwarded to subclass implementation.

    Returns:
      ildj: `Tensor`, if this bijector is injective.
        If not injective, returns the tuple of local log det
        Jacobians, `log(det(Dg_i^{-1}(y)))`, where `g_i` is the restriction
        of `g` to the `ith` partition `Di`.

    Raises:
      TypeError: if `x`'s dtype is incompatible with the expected inverse-dtype.
      NotImplementedError: if `_inverse_log_det_jacobian` is not implemented.
      ValueError: if `event_ndims` is `None` and
        `self.has_static_min_event_ndims` is `False`.
    """
    return self._call_inverse_log_det_jacobian(y, event_ndims, name, **kwargs)

  def _call_forward_log_det_jacobian(self, x, event_ndims, name, **kwargs):
    """Wraps call to _forward_log_det_jacobian, allowing extra shared logic.

    Specifically, this method
      - adds a name scope,
      - performs validations,
      - handles the special case of non-injective Bijector (forward jacobian is
        ill-defined in this case and we raise an exception)

    so that sub-classes don't have to worry about this stuff.

    Args:
      x: same as in `forward_log_det_jacobian`
      event_ndims: same as in `forward_log_det_jacobian`
      name: same as in `forward_log_det_jacobian`
      **kwargs: same as in `forward_log_det_jacobian`

    Returns:
      fldj: the forward log det jacobian at `x`. Also updates the cache as
      needed.
    """
    if not self._is_injective:
      raise NotImplementedError(
          'forward_log_det_jacobian cannot be implemented for non-injective '
          'transforms.')

    if not self.has_static_min_event_ndims:
      raise NotImplementedError(
          'Subclasses without static `forward_min_event_ndims` must override '
          '`_call_forward_log_det_jacobian`.')

    with self._name_and_control_scope(name):
      dtype = self.inverse_dtype(**kwargs)
      x = nest_util.convert_to_nested_tensor(
          x, name='x', dtype_hint=dtype,
          dtype=None if SKIP_DTYPE_CHECKS else dtype,
          allow_packing=True)

      if event_ndims is None:
        event_ndims = self.forward_min_event_ndims

      reduce_shape, assertions = ldj_reduction_shape(
          nest.map_structure(ps.shape, x),
          event_ndims=nest_util.coerce_structure(
              self.forward_min_event_ndims, event_ndims),
          min_event_ndims=self._forward_min_event_ndims,
          parameter_batch_shape=self._parameter_batch_shape,
          allow_event_shape_broadcasting=self._allow_event_shape_broadcasting,
          validate_args=self.validate_args)

      # Make sure we have validated reduce_shape before continuing on.
      with tf.control_dependencies(assertions):
        # Make sure the unreduced ILDJ is in the cache.
        attrs = self._cache.forward_attributes(x, **kwargs)
        if 'ildj' in attrs:
          ildj = attrs['ildj']
        elif hasattr(self, '_forward_log_det_jacobian'):
          ildj = attrs['ildj'] = -self._forward_log_det_jacobian(x, **kwargs)
        elif hasattr(self, '_inverse_log_det_jacobian'):
          y = self.forward(x, **kwargs)  # Fall back to computing `ildj(y)`
          ildj = attrs['ildj'] = self._inverse_log_det_jacobian(y, **kwargs)
        elif self._is_scalar:
          ildj = -_autodiff_log_det_jacobian(self._forward, x)
        else:
          raise NotImplementedError(
              'Neither _forward_log_det_jacobian nor _inverse_log_det_jacobian '
              'is implemented. One or the other is required.')

        return self._reduce_jacobian_det_over_shape(-ildj, reduce_shape)

  def forward_log_det_jacobian(self,
                               x,
                               event_ndims=None,
                               name='forward_log_det_jacobian',
                               **kwargs):
    """Returns both the forward_log_det_jacobian.

    Args:
      x: `Tensor` (structure). The input to the 'forward' Jacobian determinant
        evaluation.
      event_ndims: Optional number of dimensions in the probabilistic events
        being transformed; this must be greater than or equal to
        `self.forward_min_event_ndims`. If `event_ndims` is specified, the
        log Jacobian determinant is summed over its final
        `event_ndims - self.forward_min_event_ndims` dimensions to produce a
        scalar log-determinant for each event. Otherwise (if `event_ndims` is
        `None`), no reduction is performed.
        Multipart bijectors require *structured* event_ndims, such that
        `rank(y[i]) - rank(event_ndims[i])` is the same for all elements `i` of
        the structured input. Furthermore, the first `event_ndims[i]` of each
        `x[i].shape` must be the same for all `i` (broadcasting is not allowed).
        Multipart bijectors for which `self.has_static_min_event_ndims == False`
        require `event_ndims` to be passed explicitly, and will raise a
        `ValueError` if `event_ndims` is `None`.
        Default value: `None` (equivalent to `self.forward_min_event_ndims`).
      name: The name to give this op.
      **kwargs: Named arguments forwarded to subclass implementation.

    Returns:
      `Tensor` (structure), if this bijector is injective.
        If not injective this is not implemented.

    Raises:
      TypeError: if `y`'s dtype is incompatible with the expected output dtype.
      NotImplementedError: if neither `_forward_log_det_jacobian`
        nor {`_inverse`, `_inverse_log_det_jacobian`} are implemented, or
        this is a non-injective bijector.
      ValueError: if `event_ndims` is `None` and
        `self.has_static_min_event_ndims` is `False`.
    """
    return self._call_forward_log_det_jacobian(x, event_ndims, name, **kwargs)

  def _forward_dtype(self, input_dtype, **kwargs):
    """Subclass stub for `forward_dtype`."""
    return input_dtype

  def _inverse_dtype(self, output_dtype, **kwargs):
    """Subclass stub for `inverse_dtype`."""
    return output_dtype

  def forward_dtype(self, dtype=UNSPECIFIED, name='forward_dtype', **kwargs):
    """Returns the dtype returned by `forward` for the provided input."""
    with tf.name_scope('{}/{}'.format(self.name, name)):
      if dtype is UNSPECIFIED:
        # We pass the broadcasted input structure through `_forward_dtype`
        # rather than directly returning the output structure, allowing
        # subclasses to alter results based on `**kwargs`.
        input_dtype = nest_util.broadcast_structure(
            self.forward_min_event_ndims, self.dtype)
      else:
        # Make sure inputs are compatible with statically-known dtype.
        input_dtype = nest.map_structure_up_to(
            self.forward_min_event_ndims,
            lambda x: dtype_util.convert_to_dtype(x, dtype=self.dtype),
            nest_util.coerce_structure(self.forward_min_event_ndims, dtype),
            check_types=False)

      output_dtype = self._forward_dtype(input_dtype, **kwargs)
      try:
        # kwargs may alter dtypes themselves, but we currently require
        # structure to be statically known.
        nest.assert_same_structure(self.inverse_min_event_ndims, output_dtype,
                                   check_types=False)
      except Exception as err:
        raise NotImplementedError(
            'Changing output structure in `forward_dtype` '
            'at runtime is not currently supported:\n' + str(err))
      return output_dtype

  def inverse_dtype(self, dtype=UNSPECIFIED, name='inverse_dtype', **kwargs):
    """Returns the dtype returned by `inverse` for the provided input."""
    with tf.name_scope('{}/{}'.format(self.name, name)):
      if dtype is UNSPECIFIED:
        # We pass the broadcasted output structure through `_inverse_dtype`
        # rather than directly returning the input structure, allowing
        # subclasses to alter results based on `**kwargs`.
        output_dtype = nest_util.broadcast_structure(
            self.inverse_min_event_ndims, self.dtype)
      else:
        # Make sure inputs are compatible with statically-known dtype.
        output_dtype = nest.map_structure_up_to(
            self.inverse_min_event_ndims,
            lambda y: dtype_util.convert_to_dtype(y, dtype=self.dtype),
            nest_util.coerce_structure(self.inverse_min_event_ndims, dtype),
            check_types=False)

      input_dtype = self._inverse_dtype(output_dtype, **kwargs)
      try:
        # kwargs may alter dtypes themselves, but we currently require
        # structure to be statically known.
        nest.assert_same_structure(self.forward_min_event_ndims, input_dtype,
                                   check_types=False)
      except Exception as err:
        raise NotImplementedError(
            'Changing output structure in `inverse_dtype` '
            'at runtime is not currently supported:\n' + str(err))
      return input_dtype

  def forward_event_ndims(self, event_ndims, **kwargs):
    """Returns the number of event dimensions produced by `forward`."""
    if not self.has_static_min_event_ndims:
      raise NotImplementedError(
          'Subclasses without static min_event_ndims must override '
          '`forward_event_ndims`')
    ldj_reduce_ndims = ldj_reduction_ndims(
        nest_util.coerce_structure(self.forward_min_event_ndims, event_ndims),
        self._forward_min_event_ndims)
    return nest.map_structure(
        lambda ndims: ldj_reduce_ndims + ndims,
        self._inverse_min_event_ndims)

  def inverse_event_ndims(self, event_ndims, **kwargs):
    """Returns the number of event dimensions produced by `inverse`."""
    if not self.has_static_min_event_ndims:
      raise NotImplementedError(
          'Subclasses without static min_event_ndims must override '
          '`inverse_event_ndims`')
    ldj_reduce_ndims = ldj_reduction_ndims(
        nest_util.coerce_structure(self.inverse_min_event_ndims, event_ndims),
        self._inverse_min_event_ndims)
    return nest.map_structure(
        lambda ndims: ldj_reduce_ndims + ndims,
        self._forward_min_event_ndims)

  @contextlib.contextmanager
  def _name_and_control_scope(self, name=None):
    """Helper function to standardize op scope."""
    with tf.name_scope(self.name):
      with tf.name_scope(name) as name_scope:
        deps = tuple(
            d for d in (  # pylint: disable=g-complex-comprehension
                tuple(self._initial_parameter_control_dependencies) +
                tuple(self._parameter_control_dependencies(is_init=False)))
            if d is not None)
        if not deps:
          yield name_scope
          return
        with tf.control_dependencies(deps) as deps_scope:
          yield deps_scope

  def _reduce_jacobian_det_over_shape(self, unreduced, reduce_shape):
    """Reduce LDJ over the rightmost `reduce_shape.ndims` dimensions."""
    # Broadcast LDJ to the reduce shape (in case of is_constant_jacobian)
    # and reduce over the trailing dimensions.
    ones = tf.ones(reduce_shape, unreduced.dtype)
    reduce_dims = ps.range(-ps.size(reduce_shape), 0)
    return tf.reduce_sum(ones * unreduced, axis=reduce_dims)

  def _parameter_control_dependencies(self, is_init):
    """Returns a list of ops to be executed in members with graph deps.

    Typically subclasses override this function to return parameter specific
    assertions (eg, positivity of `scale`, etc.).

    Args:
      is_init: Python `bool` indicating that the call site is `__init__`.

    Returns:
      dependencies: `list`-like of ops to be executed in member functions with
        graph dependencies.
    """
    return ()

  @property
  def _composite_tensor_params(self):
    """A tuple describing which parameters are expected to be tensors.

    CompositeTensor requires us to partition dynamic (tensor) parts from static
    (metadata) parts like 'validate_args'.  This collects the keys of parameters
    which are expected to be tensors.
    """
    return (self._composite_tensor_nonshape_params +
            self._composite_tensor_shape_params)

  @property
  def _composite_tensor_nonshape_params(self):
    """A tuple describing which parameters are non-shape-related tensors.

    Flattening in JAX involves many of the same considerations with regards to
    identifying tensor arguments for the purposes of CompositeTensor, except
    that shape-related items will be considered metadata.  This property
    identifies the keys of parameters that are expected to be tensors, except
    those that are shape-related.
    """
    pnames = ()
    for p in self.parameters.keys():
      if p in self._composite_tensor_shape_params:
        continue
      if tf.is_tensor(getattr(self, p, None)):
        pnames += (p,)
    return pnames

  @property
  def _composite_tensor_shape_params(self):
    """A tuple describing which parameters are shape-related tensors.

    Flattening in JAX involves many of the same considerations with regards to
    identifying tensor arguments for the purposes of CompositeTensor, except
    that shape-related items will be considered metadata.  This property
    identifies the keys of parameters that are expected to be shape-related
    tensors, so that they can be collected appropriately in CompositeTensor but
    not in JAX applications.
    """
    return ()


def check_valid_ndims(ndims, validate=True):
  """Ensures that `ndims` is a non-negative integer.

  Args:
    ndims: The value to be validated and returned.
    validate: Whether to use runtime assertions when `ndims` is not known
      statically. If true, the value returned by this method is conditioned on
      `tf.debugging` ops. Otherwise, only run statically-decidable assertions.
  Returns:
    The validated `ndims`, possibly wrapped in a `tf.identity` to trigger
    any required runtime assertions.
  Raises:
    ValueError: If `ndims` is not a scalar integer.
    ValueError: If the (statically-known) `ndims` is negative.
  """
  # Container for runtime assertions, when `validate=True`.
  assertions = []

  shape = ps.shape(ndims)
  if not tf.is_tensor(shape):
    if shape.tolist():
      raise ValueError('Expected scalar, saw shape {}.'.format(shape))
  elif validate:
    assertions.append(assert_util.assert_rank_at_most(
        x=ndims, rank=0, message='Expected scalar'))

  # Dtype is *always* known statically.
  if hasattr(ndims, 'dtype'):
    if not dtype_util.is_integer(ndims.dtype):
      raise ValueError('Expected integer, got dtype {}.'.format(ndims.dtype))
  elif not isinstance(ndims, int):
    raise ValueError('Expected integer, got dtype {}.'.format(type(ndims)))

  ndims_ = tf.get_static_value(ndims)
  if ndims_ is not None:
    if not np.all(ndims_ >= 0):
      raise ValueError('`ndims` must be non-negative, saw {}.'.format(ndims_))
  elif validate:
    with tf.control_dependencies(assertions):
      assertions.append(assert_util.assert_non_negative(
          ndims, message='`ndims` must be non-negative.'))

  if assertions:
    with tf.control_dependencies(assertions):
      ndims = tf.identity(ndims)
  return ndims


def ldj_reduction_ndims(event_ndims,
                        min_event_ndims,
                        validate_args=True,
                        name='ldj_reduction_ndims'):
  """Get the unique difference between `event_ndims` and `min_event_ndims`.

  This method takes two parallel structures of integer values: namely, the
  structured user-specified `event_ndims` and the bijector's `min_event_ndims`.
  It checks that the elementwise difference between the structures is equal for
  all elements and returns the unique scalar difference.

  Args:
    event_ndims: A structure of integers representing user-specified
      `event_ndims` of a bijector input. Assumed to be positive integer values.
    min_event_ndims: A corresponding structure of integers representing the
      minimum rank of bijector inputs. We check that every element of
      `event_ndims[i] - min_event_ndims[i]` is equal, and that the difference is
      non-negative.
    validate_args: Whether to use runtime assertions when either `event_ndims`
      or `min_event_ndims` are not known statically. If true, the value returned
      by this method is conditioned on `tf.debugging` ops where required.
      Otherwise, only run statically-decidable assertions.
    name: Python `str` name given to ops created by this function.
  Returns:
    reduction_ndims: An unstructured non-negative integer representing
      the `event_ndims` additional to `min_event_ndims`, along which the log-
      det-Jacobian is later reduced.
  Raises:
    ValueError: When the structured difference between `event_ndims` and
      `min_event_ndims` is not the same for all elements.
  """
  with tf.name_scope(name):
    assertions = []

    flat_event_ndims = nest.flatten(event_ndims)
    flat_min_event_ndims = nest.flatten_up_to(event_ndims, min_event_ndims)

    flat_differences = []
    differences_all_static = True

    for dim, min_dim in zip(flat_event_ndims, flat_min_event_ndims):
      difference = dim - min_dim
      difference_ = tf.get_static_value(difference)
      if difference_ is not None:
        flat_differences.append(np.int32(difference_))
      else:
        flat_differences.append(difference)
        differences_all_static = False

    # Make sure the differences are unique.
    if len(flat_differences) > 1:
      if differences_all_static:
        if len(set(flat_differences)) > 1:
          raise ValueError(
              ('Differences between `event_ndims` and `min_event_ndims must be '
               'equal for all elements of the structured input. Saw '
               'event_ndims={}, min_event_ndims={}.'
               ).format(event_ndims, min_event_ndims))
      elif validate_args:
        with tf.control_dependencies(assertions):
          assertions.append(assert_util.assert_equal(
              flat_differences[0], flat_differences[1:],
              message=('Differences between `event_ndims` and `min_event_ndims`'
                       ' must be equal for all elements of the structured '
                       'input. Saw event_ndims={}, min_event_ndims={}.'
                       ).format(event_ndims, min_event_ndims)))

    # Now the we know they're all the same, just choose the first.
    result = flat_differences[0]
    if assertions:
      with tf.control_dependencies(assertions):
        result = tf.identity(result)
    return result


def ldj_reduction_shape(shape_structure,
                        event_ndims,
                        min_event_ndims,
                        parameter_batch_shape,
                        allow_event_shape_broadcasting=False,
                        validate_args=True,
                        name='ldj_reduction_shape'):
  """Get the shape of the LDJ reduction for a structured input.

  The `ldj_reduction_shape` is the shape of the rightmost dimensions removed
  when computing  `a_bijector.fldj(x, event_ndims)` in terms of
  `a_bijector.fldj(x, min_event_ndims)`.

  Concretely, this is the broadcasted value of
  `shape_structure[i][-event_ndims[i]:-min_event_ndims[i]]` for all elements
  `i` of the structured arguments. If `allow_event_shape_broadcasting` is False,
  then it is verified that the shape elements are identical (not only
  broadcastable). If broadcasting occurs among the shape elements, LDJ may be
  incorrect.

  For this to be valid, the following must all be true:
  1) `shape_structure`, `event_ndims`, and `min_event_ndims` have identical
     nested structure (representing valid shapes and ranks, respectively).
  2) `event_ndims[i] >= min_event_ndims[i]` for all `i` in the structured
      inputs.
  3) `reduce_ndims := event_ndims[i] - min_event_ndims[i]` takes the same value
     for all `i` in the structured inputs.
  4) `reduce_shape := shape_structure[i][-event_ndims[i]:-min_event_ndims[i]]`
     takes the same value for all `i` in the structured inputs, if
     `allow_event_shape_broadcasting` is False.

  Example Usage:
  ```
  # Standalone examples:
  ldj_reduction_shape([1, 2, 3, 4], event_ndims=2, min_event_ndims=1)
  ==> [3]

  ldj_reduction_shape({
      'a': [1, 2, 3, 4],
      'b': [1, 2, 3, 4, 5]
    },
    event_ndims={'a': 2, 'b': 3},
    min_event_ndims={'a': 1, 'b': 2})
  ==> [3]

  # Concrete example:
  unreduced_ldj = some_bijector._forward_log_det_jacobian(x)
  reduced_ldj = some_bijector.forward_log_det_jacobian(x, x_event_ndims)

  reduce_shape = ldj_reduction_shape(
    shape_structure=nest.map_structure(tf.shape, x),
    event_ndims=x_event_ndims,
    min_event_ndims=some_bijector._forward_min_event_ndims)

  assert unreduced_ldj.shape == reduced_ldj.shape + reduce_shape
  ```

  Args:
    shape_structure: A structure of shape-tensors describing input-shapes to a
      bijector.
    event_ndims: A matching structure of scalar integers used to determine the
      "requested" batch-ndims.
    min_event_ndims: A matching structure of scalar integers that are intrinsic
      properties of the bijector, used to determine the maximum possible
      batch-ndims.
    parameter_batch_shape: The broadcasted `batch_shape` of bijector parameters
      or `None`.
    allow_event_shape_broadcasting: Whether to validate that the total number of
      degrees of freedom in the event shape is unchanged by the forward/inverse
      transformations and the `ldj_reduction_shape` is the same for all elements
      of the structure. If this is violated, then LDJ may be incorrect.
    validate_args: Whether to use runtime assertions when properties of the
      arguments are not known statically. If true, the value returned by this
      method is conditioned on `tf.debugging` ops where required. Otherwise,
      only run statically-decidable assertions.
    name: Python `str` name given to ops created by this function.
      Default value: "ldj_reduction_shape".
  Returns:
    ldj_reduction_shape: A tensor shape describing the rightmost dimensions
      between `event_ndims` and `min_event_ndims` for all structured inputs.
    assertions: A list of ops that should be passed to downstream
      `tf.control_operations`. This is a *temporary* workaround to support
      keras models under TF1.
  Raises:
    ValueError: When `ndims` or `event_ndims` are not matching structures
      of valid ranks (non-negative integers).
    ValueError: When `min_event_ndims[i]` and `event_ndims[i]` describe an
      invalid slice of `shape_structure[i]` for any element of the structured
      inputs.
    ValueError: When `ldj_reduction_shape[i]` computed on any element `[i]` of
      the structured inputs is not the same across the structure if
      `allow_event_shape_broadcasting` is False.
  """
  with tf.name_scope(name):
    # Container for dynamic assertions
    assertions = []

    # Make sure event_ndims and min_event_ndims are valid and get reduce_ndims.
    min_event_ndims = nest.map_structure(
        lambda nd: check_valid_ndims(nd, validate_args), min_event_ndims)
    event_ndims = nest.map_structure_up_to(
        min_event_ndims,
        lambda nd: check_valid_ndims(nd, validate_args), event_ndims)
    reduce_ndims = ldj_reduction_ndims(
        event_ndims, min_event_ndims, validate_args=validate_args,
        name='reduce_ndims')

    # Make sure the number of dimensions we're reducing over is non-negative.
    reduce_ndims_ = tf.get_static_value(reduce_ndims)
    if reduce_ndims_ is not None:
      if reduce_ndims_ < 0:
        raise ValueError('`event_ndims must be at least {}. Saw: {}.'
                         .format(min_event_ndims, event_ndims))
    elif validate_args:
      with tf.control_dependencies(assertions):
        assertions.append(
            assert_util.assert_non_negative(
                reduce_ndims,
                message='`event_ndims` must be at least {}. Saw: {}'.format(
                    min_event_ndims, event_ndims)))

    # Make sure inputs have rank greater than event_ndims.
    rank_structure = nest.map_structure_up_to(
        event_ndims, ps.size, shape_structure)
    for rank, ndims in zip(
        nest.flatten(rank_structure), nest.flatten(event_ndims)):
      rank_ = tf.get_static_value(rank)
      ndims_ = tf.get_static_value(ndims)
      if rank_ is not None and ndims_ is not None:
        if rank_ < ndims_:
          raise ValueError('Input must have rank at least {}. Saw: {}'.format(
              ndims_, rank_))
      elif validate_args:
        with tf.control_dependencies(assertions):
          assertions.append(
              assert_util.assert_greater_equal(
                  rank, tf.cast(ndims, dtype_util.convert_to_dtype(rank)),
                  message=('Input must have rank at least {}.'
                           'Saw: {}'.format(ndims, rank))))

    # Get the non-minimal portion of the event shape over which to reduce LDJ.
    ldj_reduce_shapes = nest.flatten(
        nest.map_structure(
            lambda s, r, dim: ps.slice(  # pylint: disable=g-long-lambda
                s, begin=[r - dim], size=[reduce_ndims]),
            shape_structure, rank_structure, event_ndims))

    # Make sure that the `event_shape` dimensions to the left of
    # `min_event_ndims` are equal for all structured elements.
    # We can skip this step if the input is unstructured.
    if len(ldj_reduce_shapes) > 1 and not allow_event_shape_broadcasting:
      # Try to check shapes statically.
      ldj_reduce_shapes_ = [tf.get_static_value(s) for s in ldj_reduce_shapes]
      if not any(s is None for s in ldj_reduce_shapes_):
        if len(set(map(tuple, ldj_reduce_shapes_))) > 1:
          raise ValueError(
              '`event_shape` components to the left of `min_event_ndims` must '
              ' be equal. Saw: {}.'.format(
                  nest.pack_sequence_as(shape_structure, ldj_reduce_shapes_)))

      elif validate_args:
        with tf.control_dependencies(assertions):
          assertions.append(
              assert_util.assert_equal(
                  ldj_reduce_shapes[0], ldj_reduce_shapes[1:],
                  message=('`event_shape` components to the left of '
                           '`min_event_ndims` must be equal.')))

    ldj_reduce_shape = ldj_reduce_shapes[0]
    if allow_event_shape_broadcasting:
      for s in ldj_reduce_shapes[1:]:
        ldj_reduce_shape = ps.broadcast_shape(ldj_reduce_shape, s)

    # Check that the bijector's batch shape does not expand `ldj_reduce_shape`.
    elif parameter_batch_shape is not None:
      parameter_batch_shape_ = tf.get_static_value(parameter_batch_shape)
      ldj_reduce_shape_ = tf.get_static_value(ldj_reduce_shape)
      reduce_ndims_ = tf.get_static_value(reduce_ndims)
      if all(x is not None for x in
             (parameter_batch_shape_, ldj_reduce_shape_, reduce_ndims_)):
        if np.size(parameter_batch_shape_) <= reduce_ndims_:
          parameter_batch_in_ldj_shape_ = parameter_batch_shape_
        else:
          parameter_batch_in_ldj_shape_ = parameter_batch_shape_[
              ps.rank_from_shape(parameter_batch_shape_) - reduce_ndims_:]
        broadcasted_shape_ = tf.broadcast_static_shape(
            tf.TensorShape(parameter_batch_in_ldj_shape_),
            tf.TensorShape(ldj_reduce_shape_))
        if not np.array_equal(ldj_reduce_shape_, broadcasted_shape_):
          raise ValueError('Broadcasting with bijector parameters changes the '
                           'LDJ reduction shape from {} to {}.'.format(
                               ldj_reduce_shape_, broadcasted_shape_))
      elif validate_args:
        parameter_batch_rank = ps.size(parameter_batch_shape)
        parameter_batch_in_ldj_shape = ps.slice(
            parameter_batch_shape,
            begin=[tf.maximum(parameter_batch_rank - reduce_ndims, 0)],
            size=[tf.minimum(reduce_ndims, parameter_batch_rank)])
        with tf.control_dependencies(assertions):
          assertions.append(
              assert_util.assert_equal(
                  ldj_reduce_shape,
                  ps.broadcast_shape(
                      ldj_reduce_shape, parameter_batch_in_ldj_shape),
                  message=('Broadcasting with bijector parameters changes the '
                           'LDJ reduction shape.')))

    return ldj_reduce_shape, assertions


def _autodiff_log_det_jacobian(fn, x):
  """Automatically compute the log det jacobian of a scalar function."""
  _, grads = gradient.value_and_gradient(fn, x)
  if grads is None:
    raise ValueError('Cannot compute log det jacobian; function {} has `None` '
                     'gradient.'.format(fn))
  return tf.math.log(tf.abs(grads))
