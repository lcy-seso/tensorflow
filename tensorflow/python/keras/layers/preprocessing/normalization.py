# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Keras preprocessing layers."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json

import numpy as np

from tensorflow.python.framework import dtypes
from tensorflow.python.keras import backend as K
from tensorflow.python.keras.engine.base_preprocessing_layer import Combiner
from tensorflow.python.keras.engine.base_preprocessing_layer import CombinerPreprocessingLayer
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.util import compat
from tensorflow.python.util.tf_export import keras_export

_COUNT_NAME = 'count'
_MEAN_NAME = 'mean'
_VARIANCE_NAME = 'variance'


# TODO(momernick): Find a good example of normalization?
@keras_export('keras.layers.experimental.preprocessing.Normalization', v1=[])
class Normalization(CombinerPreprocessingLayer):
  """Feature-wise normalization of the data.

  This layer will coerce its inputs into a distribution centered around
  0 with standard deviation 1. It accomplishes this by precomputing the mean and
  variance of the data, and calling (input-mean)/sqrt(var) at runtime.

  What happens in `adapt`: Compute mean and variance of the data and store them
    as the layer's weights. `adapt` should be called before `fit`, `evaluate`,
    or `predict`.

  Attributes:
      axis: Integer or tuple of integers, the axis or axes that should be
        normalized (typically the features axis). We will normalize each element
        in the specified axis. If set to 'None', the layer will perform scalar
        normalization (diving the input by a single scalar value). 0 (the batch
        axis) is not allowed.


  Examples:

  Calculate the mean and variance by analyzing the dataset in `adapt`.

  >>> adapt_data = np.array([[1.], [2.], [3.], [4.], [5.]], dtype=np.float32)
  >>> input_data = np.array([[1.], [2.], [3.]], np.float32)
  >>> layer = Normalization()
  >>> layer.adapt(adapt_data)
  >>> layer(input_data)
  <tf.Tensor: shape=(3, 1), dtype=float32, numpy=
  array([[-1.4142135 ],
         [-0.70710677],
         [ 0.        ]], dtype=float32)>
  """

  def __init__(self, axis=-1, dtype=None, **kwargs):
    # This ensures that if the value of K.floatx() changes after file-loading
    # time, the dtype value will change to reflect it.
    dtype = dtype or K.floatx()

    super(Normalization, self).__init__(
        combiner=_NormalizingCombiner(axis), dtype=dtype, **kwargs)

    if axis == 0:
      raise ValueError('The argument \'axis\' may not be 0.')

    self.axis = axis

  def build(self, input_shape):

    self._broadcast_shape = [1 for _ in range(len(input_shape))]
    if isinstance(self.axis, (tuple, list)):
      mean_and_var_shape = []
      for i in self.axis:
        mean_and_var_shape.append(input_shape[i])
        self._broadcast_shape[i] = input_shape[i]
    else:
      if self.axis is None:
        mean_and_var_shape = ()
      else:
        mean_and_var_shape = input_shape[self.axis]
        self._broadcast_shape[self.axis] = input_shape[self.axis]

    # count is not used in this class's call() method, but is used to re-create
    # the accumulator during multiple calls to 'adapt'.
    # TODO(omalleyt): should mean and variance be set to self.dtype?
    self.mean = self._add_state_variable(
        name=_MEAN_NAME,
        shape=mean_and_var_shape,
        dtype=K.floatx(),
        initializer=init_ops.zeros_initializer)
    self.variance = self._add_state_variable(
        name=_VARIANCE_NAME,
        shape=mean_and_var_shape,
        dtype=K.floatx(),
        initializer=init_ops.ones_initializer)
    self.count = self._add_state_variable(
        name=_COUNT_NAME,
        shape=(),
        dtype=dtypes.int64,
        initializer=init_ops.zeros_initializer)

    super(Normalization, self).build(input_shape)

  def call(self, inputs):
    # If the inputs are not floats, cast them to floats. This avoids issues
    # with int-float multiplication and division below.
    if inputs.dtype != K.floatx():
      inputs = math_ops.cast(inputs, K.floatx())
    # We need to reshape the mean and variance data to ensure that Tensorflow
    # broadcasts the data correctly.
    mean = array_ops.reshape(self.mean, self._broadcast_shape)
    variance = array_ops.reshape(self.variance, self._broadcast_shape)
    return (inputs - mean) / math_ops.sqrt(variance)

  def compute_output_shape(self, input_shape):
    return input_shape

  def compute_output_signature(self, input_spec):
    return input_spec

  def get_config(self):
    config = {'axis': self.axis}
    base_config = super(Normalization, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def set_weights(self, weights):
    """Override for set_weights to ensure we can set just mean/var weights."""
    if len(weights) == 2:
      weights.append(np.array(0))
    super(Normalization, self).set_weights(weights)


class _NormalizingCombiner(Combiner):
  """Combiner for the Normalization preprocessing layer.

  This class encapsulates the computations for finding the mean and variance
  of a set of data in a stable and numerically correct way. Its associated
  accumulator is a namedtuple('count', 'mean', 'variance').

  Attributes:
    axis: The axis to compute mean and var over.
  """
  COUNT_IDX = 0
  MEAN_IDX = 1
  VAR_IDX = 2

  def __init__(self, axis):
    self.axis = axis

  def compute(self, values, accumulator=None):
    """Compute a step in this computation, returning a new accumulator."""

    # This is the shape of all reduced axes (not specified in 'axis').
    if self.axis is None:
      reduction_counts = values.shape
    else:
      reduction_counts = np.delete(values.shape, self.axis)
    # We get the number of elements that will be reduced by multiplying all
    # values of 'shape' corresponding to the reduced axes.
    count = np.prod(reduction_counts, dtype=np.int32)

    # We want to reduce across dimensions except those specified in 'axis'
    # when using np.mean or np.variance; create the tuple of axes to reduce
    # over here.
    if self.axis is None:
      reduction_axes = None
    else:
      reduction_axes = tuple(np.delete(range(values.ndim), self.axis))

    mean = np.mean(values, axis=reduction_axes, dtype=np.float64)
    variance = np.var(values, axis=reduction_axes, dtype=np.float64)

    # Create an accumulator with our new data and either return it or combine
    # it with the passed accumulator.
    if accumulator is None:
      return self._create_accumulator(count, mean, variance)
    else:
      return self.add_data_to_accumulator(count, mean, variance, accumulator)

  def add_data_to_accumulator(self, count, mean, variance, accumulator):
    """Add new data to the totals in an accumulator."""
    # Combine accumulators and return the result.
    combined_count = count + accumulator[self.COUNT_IDX]

    # To combine accumulator means, we weight each accumulator's mean by the
    # number of elements that were accumulated, and then divide by the
    # total number of elements.
    combined_mean = (mean * count + accumulator[self.MEAN_IDX] *
                     accumulator[self.COUNT_IDX]) / combined_count

    # The variance is computed using the lack-of-fit sum of squares
    # formula (see https://en.wikipedia.org/wiki/Lack-of-fit_sum_of_squares).
    accumulator_var_contribution = accumulator[self.COUNT_IDX] * (
        accumulator[self.VAR_IDX] +
        np.square(accumulator[self.MEAN_IDX] - combined_mean))
    data_var_contribution = count * (variance + np.square(mean - combined_mean))
    combined_variance = (accumulator_var_contribution +
                         data_var_contribution) / combined_count

    accumulator[self.COUNT_IDX] = combined_count
    accumulator[self.MEAN_IDX] = np.nan_to_num(combined_mean)
    accumulator[self.VAR_IDX] = np.nan_to_num(combined_variance)
    return accumulator

  def merge(self, accumulators):
    """Merge several accumulators to a single accumulator."""
    # Combine accumulators and return the result.
    combined_count = np.sum(
        [accumulator[self.COUNT_IDX] for accumulator in accumulators])

    # To combine accumulator means, we weight each accumulator's mean by the
    # number of elements that were accumulated, and then divide by the
    # total number of elements.
    combined_mean = np.add.reduce([
        accumulator[self.MEAN_IDX] * accumulator[self.COUNT_IDX]
        for accumulator in accumulators
    ]) / combined_count

    # The variance is computed using the lack-of-fit sum of squares
    # formula (see https://en.wikipedia.org/wiki/Lack-of-fit_sum_of_squares).
    def variance_contribution(accumulator):
      return accumulator[self.COUNT_IDX] * (
          accumulator[self.VAR_IDX] +
          np.square(accumulator[self.MEAN_IDX] - combined_mean))

    combined_variance = np.add.reduce([
        variance_contribution(accumulator) for accumulator in accumulators
    ]) / combined_count

    return self._create_accumulator(combined_count, combined_mean,
                                    combined_variance)

  def extract(self, accumulator):
    """Convert an accumulator into a dict of output values."""
    return {
        _COUNT_NAME: accumulator[self.COUNT_IDX],
        _MEAN_NAME: accumulator[1],
        _VARIANCE_NAME: accumulator[2]
    }

  def restore(self, output):
    """Create an accumulator based on 'output'."""
    # There is no special internal state here, so we just return the relevant
    # internal value.
    count = output[_COUNT_NAME]
    mean = output[_MEAN_NAME]
    var = output[_VARIANCE_NAME]
    if (count == 0 and (mean.any() != 0.0 or var.any() != 0.0)):
      raise RuntimeError(
          'The mean and/or variance of a Normalization preprocessing layer '
          "were set without also setting 'count'. If 'count' is not also set,"
          " 'adapt' cannot be called unless the 'reset_state' arg is True.")
    return self._create_accumulator(output[_COUNT_NAME], output[_MEAN_NAME],
                                    output[_VARIANCE_NAME])

  def serialize(self, accumulator):
    """Serialize an accumulator for a remote call."""
    output_dict = {
        _COUNT_NAME: accumulator[self.COUNT_IDX].tolist(),
        _MEAN_NAME: accumulator[1].tolist(),
        _VARIANCE_NAME: accumulator[2].tolist()
    }
    return compat.as_bytes(json.dumps(output_dict))

  def deserialize(self, encoded_accumulator):
    """Deserialize an accumulator received from 'serialize()'."""
    value_dict = json.loads(compat.as_text(encoded_accumulator))
    return self._create_accumulator(
        np.array(value_dict[_COUNT_NAME]), np.array(value_dict[_MEAN_NAME]),
        np.array(value_dict[_VARIANCE_NAME]))

  def _create_accumulator(self, count, mean, variance):
    """Convert any 'nan' values in the given accumulator to numeric values."""
    return [count, mean, variance]
