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
"""The `JointDistributionCoroutine` class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import warnings

import tensorflow.compat.v2 as tf

from tensorflow_probability.python.distributions import joint_distribution as joint_distribution_lib
from tensorflow_probability.python.internal import assert_util
from tensorflow_probability.python.internal import prefer_static
from tensorflow_probability.python.internal import samplers

from tensorflow_probability.python.internal import structural_tuple
from tensorflow_probability.python.util.seed_stream import SeedStream
from tensorflow_probability.python.util.seed_stream import TENSOR_SEED_MSG_PREFIX
from tensorflow.python.util import nest  # pylint: disable=g-direct-tensorflow-import


__all__ = [
    'JointDistributionCoroutine',
]


JAX_MODE = False

# Cause all warnings to always be triggered.
# Not having this means subsequent calls wont trigger the warning.
warnings.filterwarnings(
    'always',
    module='tensorflow_probability.*joint_distribution_coroutine',
    append=True)  # Don't override user-set filters.


class JointDistributionCoroutine(joint_distribution_lib.JointDistribution):
  """Joint distribution parameterized by a distribution-making generator.

  This distribution enables both sampling and joint probability computation from
  a single model specification.

  A joint distribution is a collection of possibly interdependent distributions.
  The `JointDistributionCoroutine` is specified by a generator that
  generates the elements of this collection.

  #### Mathematical Details

  The `JointDistributionCoroutine` implements the chain rule of probability.
  That is, the probability function of a length-`d` vector `x` is,

  ```none
  p(x) = prod{ p(x[i] | x[:i]) : i = 0, ..., (d - 1) }
  ```

  The `JointDistributionCoroutine` is parameterized by a generator
  that yields `tfp.distributions.Distribution`-like instances.

  Each element yielded implements the `i`-th *full conditional distribution*,
  `p(x[i] | x[:i])`. Within the generator, the return value from the yield
  is a sample from the distribution that may be used to construct subsequent
  yielded `Distribution`-like instances. This allows later instances
  to be conditional on earlier ones.

  When the `sample` method for a `JointDistributionCoroutine` is called with
  a `sample_shape`, the `sample` method for each of the yielded
  distributions is called.
  The distributions that have been wrapped in the
  `JointDistributionCoroutine.Root` class will be called with `sample_shape` as
  the `sample_shape` argument, and the unwrapped distributions
  will be called with `()` as the `sample_shape` argument.

  It is the user's responsibility to ensure that
  each of the distributions generates samples with the specified sample
  size.

  **Name resolution**: The names of `JointDistributionCoroutine` components
  may be specified by passing `name` arguments to distribution constructors (
  `tfd.Normal(0., 1., name='x')). Components without an explicit name will be
  assigned a dummy name.

  #### Examples

  ```python
  tfd = tfp.distributions

  # Consider the following generative model:
  #     e ~ Exponential(rate=[100, 120])
  #     g ~ Gamma(concentration=e[0], rate=e[1])
  #     n ~ Normal(loc=0, scale=2.)
  #     m ~ Normal(loc=n, scale=g)

  # In TFP, we can write this as:
  Root = tfd.JointDistributionCoroutine.Root  # Convenient alias.
  def model():
    e = yield Root(tfd.Independent(tfd.Exponential(rate=[100, 120]), 1))
    g = yield tfd.Gamma(concentration=e[..., 0], rate=e[..., 1])
    n = yield Root(tfd.Normal(loc=0, scale=2.))
    m = yield tfd.Normal(loc=n, scale=g)

  joint = tfd.JointDistributionCoroutine(model)

  x = joint.sample()
  # ==> x is a length-4 tuple of Tensors representing a draw/realization from
  #     each distribution.
  joint.log_prob(x)
  # ==> A scalar `Tensor` representing the total log prob under all four
  #     distributions.
  ```

  For improved readability of sampled values, the yielded distributions can also
  be named:

  ```python
  tfd = tfp.distributions

  Root = tfd.JointDistributionCoroutine.Root  # Convenient alias.
  def model():
    e = yield Root(tfd.Independent(
        tfd.Exponential(rate=[100, 120]), 1), name='e')
    g = yield tfd.Gamma(concentration=e[..., 0], rate=e[..., 1], name='g')
    n = yield Root(tfd.Normal(loc=0, scale=2., name='n'))
    m = yield tfd.Normal(loc=n, scale=g, name='m')

  joint = tfd.JointDistributionCoroutine(model)

  x = joint.sample()
  # ==> x is a namedtuple with fields (in order) 'e', 'g', 'n', 'm' and values
  #     representing the draw/realization from each corresponding distribution.
  joint.log_prob(x)
  # ==> A scalar `Tensor` representing the total log prob under all four
  #     distributions.

  # Passing dictionaries via `kwargs` also works.
  joint.log_prob(**x._as_dict())
  # Or:
  joint.log_prob(e=..., g=..., n=..., m=...)
  ```

  If any of the yielded distributions are not explicitly named, they will
  automatically be given a name of the form `var#` where `#` is the index of the
  associated distribution. E.g. the first yielded distribution will have a
  default name of `var0`.


  #### Discussion

  Each element yielded by the generator must be a `tfd.Distribution`-like
  instance.

  An object is deemed '`tfd.Distribution`-like' if it has a
  `sample`, `log_prob`, and distribution properties, e.g., `batch_shape`,
  `event_shape`, `dtype`.

  Consider the following fragment from a generator:

  ```python
    n = yield Root(tfd.Normal(loc=0, scale=2.))
    m = yield tfd.Normal(loc=n, scale=1.0)
  ```

  The random variable `n` has no dependence on earlier random variables and
  `Root` is used to indicate that its distribution needs to be passed a
  `sample_shape`. On the other hand, the distribution of `m` is constructed
  using the value of `n`. This means that `n` is already shaped according to
  the `sample_shape` and there is no need to pass `m`'s distribution a
  `sample_size`. So `Root` is not used to wrap `m`'s distribution.

  **Note**: unlike most other distributions in `tfp.distributions`,
  `JointDistributionCoroutine.sample` returns a `tuple` of `Tensor`s
  rather than a `Tensor`.  Accordingly `joint.batch_shape` returns a
  `tuple` of `TensorShape`s for each of the distributions' batch shapes
  and `joint.batch_shape_tensor()` returns a `tuple` of `Tensor`s for
  each of the distributions' event shapes. (Same with `event_shape` analogues.)
  """

  class Root(collections.namedtuple('Root', ['distribution'])):
    """Wrapper for coroutine distributions which lack distribution parents."""
    __slots__ = ()

  def __init__(
      self,
      model,
      sample_dtype=None,
      validate_args=False,
      name=None,
  ):
    """Construct the `JointDistributionCoroutine` distribution.

    Args:
      model: A generator that yields a sequence of `tfd.Distribution`-like
        instances.
      sample_dtype: Samples from this distribution will be structured like
        `tf.nest.pack_sequence_as(sample_dtype, list_)`. `sample_dtype` is only
        used for `tf.nest.pack_sequence_as` structuring of outputs, never
        casting (which is the responsibility of the component distributions).
        Default value: `None` (i.e. `namedtuple`).
      validate_args: Python `bool`.  Whether to validate input with asserts.
        If `validate_args` is `False`, and the inputs are invalid,
        correct behavior is not guaranteed.
        Default value: `False`.
      name: The name for ops managed by the distribution.
        Default value: `None` (i.e., `JointDistributionCoroutine`).
    """
    parameters = dict(locals())
    with tf.name_scope(name or 'JointDistributionCoroutine') as name:
      self._model_coroutine = model
      # Hint `no_dependency` to tell tf.Module not to screw up the sample dtype
      # with extraneous wrapping (list => ListWrapper, etc.).
      self._sample_dtype = self._no_dependency(sample_dtype)
      self._single_sample_distributions = {}
      super(JointDistributionCoroutine, self).__init__(
          dtype=sample_dtype,
          reparameterization_type=None,  # Ignored; we'll override.
          validate_args=validate_args,
          allow_nan_stats=False,
          parameters=parameters,
          name=name)

  # TODO(b/166658748): Once the bug is resolved, we should be able to eliminate
  #   this workaround that disables sanitize_seed for JD*AB.
  _stateful_to_stateless = True

  @property
  def _require_root(self):
    return True

  @property
  def model(self):
    return self._model_coroutine

  def _assert_compatible_shape(self, index, sample_shape, samples):
    requested_shape, _ = self._expand_sample_shape_to_vector(
        tf.convert_to_tensor(sample_shape, dtype=tf.int32),
        name='requested_shape')
    actual_shape = prefer_static.shape(samples)
    actual_rank = prefer_static.rank_from_shape(actual_shape)
    requested_rank = prefer_static.rank_from_shape(requested_shape)

    # We test for two properties we expect of yielded distributions:
    # (1) The rank of the tensor of generated samples must be at least
    #     as large as the rank requested.
    # (2) The requested shape must be a prefix of the shape of the
    #     generated tensor of samples.
    # We attempt to perform test (1) statically first.
    # We don't need to do this explicitly for test (2) because
    # `assert_equal` evaluates statically if it can.
    static_actual_rank = tf.get_static_value(actual_rank)
    static_requested_rank = tf.get_static_value(requested_rank)

    assertion_message = ('Samples yielded by distribution #{} are not '
                         'consistent with `sample_shape` passed to '
                         '`JointDistributionCoroutine` '
                         'distribution.'.format(index))

    # TODO Remove this static check (b/138738650)
    if (static_actual_rank is not None and
        static_requested_rank is not None):
      # We're able to statically check the rank
      if static_actual_rank < static_requested_rank:
        raise ValueError(assertion_message)
      else:
        control_dependencies = []
    else:
      # We're not able to statically check the rank
      control_dependencies = [
          assert_util.assert_greater_equal(
              actual_rank, requested_rank,
              message=assertion_message)
          ]

    with tf.control_dependencies(control_dependencies):
      trimmed_actual_shape = actual_shape[:requested_rank]

    control_dependencies = [
        assert_util.assert_equal(
            requested_shape, trimmed_actual_shape,
            message=assertion_message)
    ]

    return control_dependencies

  def _flat_sample_distributions(self, sample_shape=(), seed=None, value=None):
    """Executes `model`, creating both samples and distributions."""
    ds = []
    values_out = []
    if samplers.is_stateful_seed(seed):
      seed_stream = SeedStream(seed, salt='JointDistributionCoroutine')
      if not self._stateful_to_stateless:
        seed = None
    else:
      seed_stream = None  # We got a stateless seed for seed=.

    # TODO(b/166658748): Make _stateful_to_stateless always True (eliminate it).
    if self._stateful_to_stateless and (seed is not None or not JAX_MODE):
      seed = samplers.sanitize_seed(seed, salt='JointDistributionCoroutine')
    gen = self._model_coroutine()
    index = 0
    d = next(gen)
    if self._require_root and not isinstance(d, self.Root):
      raise ValueError('First distribution yielded by coroutine must '
                       'be wrapped in `Root`.')
    try:
      while True:
        actual_distribution = d.distribution if isinstance(d, self.Root) else d
        ds.append(actual_distribution)
        # Ensure reproducibility even when xs are (partially) set. Always split.
        stateful_sample_seed = None if seed_stream is None else seed_stream()
        if seed is None:
          stateless_sample_seed = None
        else:
          stateless_sample_seed, seed = samplers.split_seed(seed)

        if (value is not None and len(value) > index and
            value[index] is not None):

          def convert_tree_to_tensor(x, dtype_hint):
            return tf.convert_to_tensor(x, dtype_hint=dtype_hint)

          # This signature does not allow kwarg names. Applies
          # `convert_to_tensor` on the next value.
          next_value = nest.map_structure_up_to(
              ds[-1].dtype,  # shallow_tree
              convert_tree_to_tensor,  # func
              value[index],  # x
              ds[-1].dtype)  # dtype_hint
        else:
          try:
            next_value = actual_distribution.sample(
                sample_shape=sample_shape if isinstance(d, self.Root) else (),
                seed=(stateful_sample_seed if stateless_sample_seed is None
                      else stateless_sample_seed))
          except TypeError as e:
            if ('Expected int for argument' not in str(e) and
                TENSOR_SEED_MSG_PREFIX not in str(e)) or (
                    stateful_sample_seed is None):
              raise
            msg = (
                'Falling back to stateful sampling for distribution #{index} '
                '(0-based) of type `{dist_cls}` with component name '
                '{component_name} and `dist.name` "{dist_name}". Please '
                'update to use `tf.random.stateless_*` RNGs. This fallback may '
                'be removed after 20-Dec-2020. ({exc})')
            component_name = (
                joint_distribution_lib.get_explicit_name_for_component(ds[-1]))
            if component_name is None:
              component_name = '[None specified]'
            else:
              component_name = '"{}"'.format(component_name)
            warnings.warn(msg.format(
                index=index,
                component_name=component_name,
                dist_name=ds[-1].name,
                dist_cls=type(ds[-1]),
                exc=str(e)))
            next_value = actual_distribution.sample(
                sample_shape=sample_shape if isinstance(d, self.Root) else (),
                seed=stateful_sample_seed)

        if self._validate_args:
          with tf.control_dependencies(
              self._assert_compatible_shape(
                  index, sample_shape, next_value)):
            values_out.append(tf.nest.map_structure(tf.identity, next_value))
        else:
          values_out.append(next_value)

        index += 1
        d = gen.send(next_value)
    except StopIteration:
      pass
    return ds, values_out

  def _model_unflatten(self, xs):
    if self._sample_dtype is None:
      return structural_tuple.structtuple(self._flat_resolve_names())(*xs)
    # Cast `xs` as `tuple` so we can handle generators.
    return tf.nest.pack_sequence_as(self._sample_dtype, tuple(xs))

  def _model_flatten(self, xs):
    if self._sample_dtype is None:
      return tuple((xs[k] for k in self._flat_resolve_names())
                   if isinstance(xs, collections.Mapping) else xs)
    return nest.flatten_up_to(self._sample_dtype, xs)
