# Copyright 2020 The TensorFlow Probability Authors.
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
"""Tests for tensorflow_probability.python.experimental.distribute.sharded."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow.compat.v2 as tf
import tensorflow_probability as tfp
from tensorflow_probability.python.experimental.distribute import distribute_test_lib as test_lib
from tensorflow_probability.python.experimental.distribute import sharded
from tensorflow_probability.python.internal import test_util

tfd = tfp.distributions
tfde = tfp.experimental.distributions
tfp_dist = tfp.experimental.distribute

JAX_MODE = False


@test_util.test_all_tf_execution_regimes
class ShardTest(test_lib.DistributedTest):

  def test_sharded_sample_samples_differently_across_shards(self):

    @tf.function(autograph=False)
    def run(key):
      return sharded.Sharded(
          tfd.Sample(tfd.Normal(0., 1.), 1),
          shard_axis_name=self.axis_name).sample(seed=key)

    sample = self.evaluate(
        self.per_replica_to_tensor(
            self.strategy_run(run, (self.key,), in_axes=None)))
    for i in range(4):
      for j in range(4):
        if i == j:
          continue
        self.assertNotEqual(sample[i], sample[j])

  def test_sharded_samples_differently_with_nested_axes(self):
    if not JAX_MODE:
      self.skipTest('Multiple axes only supported in JAX backend.')

    other_axis_name = self.axis_name + '_other'

    def run(key, _):
      return sharded.Sharded(
          tfd.Sample(tfd.Normal(0., 1.), 1),
          shard_axis_name=[self.axis_name, other_axis_name]).sample(seed=key)

    def outer_run(key, x):
      return self.strategy_run(run, (key, x),
                               axis_name=other_axis_name,
                               in_axes=(None, 0))
    # Pass a dummy ones([2, 2]) to create the right axis sizes
    sample = self.strategy_run(outer_run, (self.key, tf.ones([2, 2])),
                               in_axes=(None, 0))
    for i in range(2):
      for j in range(2):
        for k in range(2):
          for l in range(2):
            if i == k and j == l:
              continue
            self.assertNotEqual(sample[i, j], sample[k, l])

  def test_nested_sharded_maintains_correct_axis_ordering(self):
    if not JAX_MODE:
      self.skipTest('Multiple axes only supported in JAX backend.')

    other_axis_name = self.axis_name + '_other'

    dist = sharded.Sharded(
        sharded.Sharded(tfd.Normal(0., 1.), self.axis_name),
        other_axis_name)

    self.assertListEqual(
        dist.experimental_shard_axis_names, [self.axis_name, other_axis_name])

  def test_sharded_independent_samples_differently_across_shards(self):

    @tf.function(autograph=False)
    def run(key):
      return tfp_dist.Sharded(
          tfd.Independent(tfd.Normal(tf.zeros(1), tf.ones(1)), 1),
          shard_axis_name=self.axis_name).sample(seed=key)

    sample = self.evaluate(
        self.per_replica_to_tensor(
            self.strategy_run(run, (self.key,), in_axes=None)))
    for i in range(4):
      for j in range(4):
        if i == j:
          continue
        self.assertNotEqual(sample[i], sample[j])

  def test_kahan_custom_grad(self):

    def model_fn():
      root = tfp.experimental.distribute.JointDistributionCoroutine.Root
      _ = yield root(
          sharded.Sharded(
              tfd.Independent(
                  tfd.Normal(0, tf.ones([7])),
                  reinterpreted_batch_ndims=1,
                  experimental_use_kahan_sum=True),
              shard_axis_name=self.axis_name))

    model = tfp.experimental.distribute.JointDistributionCoroutine(model_fn)

    samps = self.strategy_run(
        lambda seed: model.sample(seed=seed),
        (test_util.test_seed(sampler_type='stateless'),),
        in_axes=None)

    @tf.function(jit_compile=True)
    def lp_grad(x):
      return tfp.math.value_and_gradient(model.log_prob, x)

    self.evaluate(
        self.per_replica_to_tensor(self.strategy_run(lp_grad, (samps,))))

  def test_log_prob(self):

    @tf.function
    def lp_grad(x):
      lp1, g1 = tfp.math.value_and_gradient(
          tfp_dist.Sharded(
              tfd.Sample(tfd.Normal(0., 1.), 1),
              shard_axis_name=self.axis_name).log_prob, (x,))
      lp2, g2 = tfp.math.value_and_gradient(
          tfp_dist.Sharded(
              tfd.Independent(tfd.Normal(tf.zeros([1]), 1.), 1),
              shard_axis_name=self.axis_name).log_prob, (x,))
      return lp1, g1, lp2, g2

    def true_lp_grad(x):
      lp1, g1 = tfp.math.value_and_gradient(
          tfd.Sample(tfd.Normal(0., 1.), test_lib.NUM_DEVICES).log_prob, (x,))
      lp2, g2 = tfp.math.value_and_gradient(
          tfd.Independent(tfd.Normal(tf.zeros([test_lib.NUM_DEVICES]), 1.),
                          1).log_prob, (x,))
      return lp1, g1, lp2, g2

    x = tf.ones([test_lib.NUM_DEVICES])
    sharded_x = self.shard_values(x)

    lp1, g1, lp2, g2 = self.evaluate(
        self.per_replica_to_tensor(self.strategy_run(lp_grad, (sharded_x,))))
    true_lp1, true_g1, true_lp2, true_g2 = self.evaluate(true_lp_grad(x))

    self.assertAllClose(true_lp1, lp1[0])
    self.assertAllClose(true_g1, g1)
    self.assertAllClose(true_lp2, lp2[0])
    self.assertAllClose(true_g2, g2)

  def test_log_prob_with_multiple_axes(self):
    if not JAX_MODE:
      self.skipTest('Multiple axes only supported in JAX backend.')

    other_axis_name = self.axis_name + '_other'

    @tf.function
    def lp_grad(x):
      lp1, g1 = tfp.math.value_and_gradient(
          tfp_dist.Sharded(
              tfd.Sample(tfd.Normal(0., 1.), 1),
              shard_axis_name=[self.axis_name, other_axis_name]).log_prob, (x,))
      lp2, g2 = tfp.math.value_and_gradient(
          tfp_dist.Sharded(
              tfd.Independent(tfd.Normal(tf.zeros([1]), 1.), 1),
              shard_axis_name=[self.axis_name, other_axis_name]).log_prob, (x,))
      return lp1, g1, lp2, g2

    def true_lp_grad(x):
      lp1, g1 = tfp.math.value_and_gradient(
          tfd.Sample(tfd.Normal(0., 1.), [2, 2]).log_prob, (x,))
      lp2, g2 = tfp.math.value_and_gradient(
          tfd.Independent(tfd.Normal(tf.zeros([2, 2]), 1.),
                          2).log_prob, (x,))
      return lp1, g1, lp2, g2

    x = tf.ones([2, 2])

    def outer_run(x):
      return self.strategy_run(lp_grad, (x,), axis_name=other_axis_name)
    lp1, g1, lp2, g2 = self.strategy_run(outer_run, (x,))
    true_lp1, true_g1, true_lp2, true_g2 = true_lp_grad(x)

    self.assertAllClose(tf.ones([2, 2]) * true_lp1, lp1)
    self.assertAllClose(true_g1[0], g1[0])
    self.assertAllClose(tf.ones([2, 2]) * true_lp2, lp2)
    self.assertAllClose(true_g2[0], g2[0])

  def test_log_prob_ratio_sample(self):
    dist1 = tfd.Sample(tfd.Normal(0., 4.), test_lib.NUM_DEVICES)
    dist2 = tfd.Sample(tfd.Normal(0., 2.), test_lib.NUM_DEVICES)

    x0 = tf.zeros([test_lib.NUM_DEVICES])
    x1 = tf.ones([test_lib.NUM_DEVICES])

    sharded_x0 = self.shard_values(x0)
    sharded_x1 = self.shard_values(x1)

    true_diff, (true_g1, true_g2) = tfp.math.value_and_gradient(
        lambda x0, x1: tfde.log_prob_ratio(dist1, x0, dist2, x1), (x0, x1))

    @tf.function
    def test(x0, x1):
      dist_sharded1 = tfp_dist.Sharded(
          tfd.Sample(tfd.Normal(0., 4.), 1),
          shard_axis_name=self.axis_name)
      dist_sharded2 = tfp_dist.Sharded(
          tfd.Sample(tfd.Normal(0., 2.), 1),
          shard_axis_name=self.axis_name)
      return (
          tfp.math.value_and_gradient(
              lambda x0, x1: tfde.log_prob_ratio(  # pylint: disable=g-long-lambda
                  dist_sharded1, x0, dist_sharded2, x1),
              (x0, x1)),
          dist_sharded1.log_prob(x0) - dist_sharded2.log_prob(x1))

    (diff1, (g1, g2)), diff2 = self.per_replica_to_tensor(
        self.strategy_run(test, (sharded_x0, sharded_x1)))

    self.assertAllClose(true_diff, diff1[0])
    self.assertAllClose(true_diff, diff2[0])
    self.assertAllClose(true_g1, g1)
    self.assertAllClose(true_g2, g2)

  def test_log_prob_ratio_independent(self):
    dist1 = tfd.Independent(tfd.Normal(tf.zeros([test_lib.NUM_DEVICES]), 2.), 1)
    dist2 = tfd.Independent(tfd.Normal(tf.zeros([test_lib.NUM_DEVICES]), 4.), 1)

    x0 = tf.zeros([test_lib.NUM_DEVICES])
    x1 = tf.ones([test_lib.NUM_DEVICES])

    sharded_x0 = self.shard_values(x0)
    sharded_x1 = self.shard_values(x1)

    true_diff, (true_g1, true_g2) = tfp.math.value_and_gradient(
        lambda x0, x1: tfde.log_prob_ratio(dist1, x0, dist2, x1), (x0, x1))

    @tf.function
    def test(x0, x1):
      dist_sharded1 = tfp_dist.Sharded(
          tfd.Independent(tfd.Normal(tf.zeros([1]), 2.), 1),
          shard_axis_name=self.axis_name)
      dist_sharded2 = tfp_dist.Sharded(
          tfd.Independent(tfd.Normal(tf.zeros([1]), 4.), 1),
          shard_axis_name=self.axis_name)
      return (
          tfp.math.value_and_gradient(
              lambda x0, x1: tfde.log_prob_ratio(  # pylint: disable=g-long-lambda
                  dist_sharded1, x0, dist_sharded2, x1),
              (x0, x1)),
          dist_sharded1.log_prob(x0) - dist_sharded2.log_prob(x1))

    (diff1, (g1, g2)), diff2 = self.per_replica_to_tensor(
        self.strategy_run(test, (sharded_x0, sharded_x1)))

    self.assertAllClose(true_diff, diff1[0])
    self.assertAllClose(true_diff, diff2[0])
    self.assertAllClose(true_g1, g1)
    self.assertAllClose(true_g2, g2)


if __name__ == '__main__':
  tf.test.main()
