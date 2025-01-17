# SPDX-FileCopyrightText: Copyright (c) 2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Distributed Embedding layers and utils"""
import math
import numpy as np
import tensorflow as tf
from tensorflow.python.keras.utils import tf_utils
import horovod.tensorflow as hvd
from distributed_embeddings.python.ops.embedding_lookup_ops import read_var_no_copy
from .embedding import Embedding


class DistEmbeddingStrategy():
  """Distributed embedding strategy

  Args:
    embeddings (list of Embedding): list of unbuilt Embedding layers globally
    strategy (str): A string indicates how embedding tables are distributed.
        Choices are [“basic”, “memory_balanced”]. Default "basic"
    input_table_map (list or None): A list of table ids mapping each input to a table, i.e.,
        `input[i]` map to `table[input_table_map[i]]`. None means there are same number of
        inputs/tables and `input[i]` map to `table[i]`. Default None.
  """

  def __init__(self,
               embeddings,
               world_size,
               rank,
               strategy="basic",
               input_table_map=None,
               column_slice_threshold=None):
    self.global_configs = [e.get_config() for e in embeddings]
    self.strategy = strategy
    if input_table_map is None:
      input_table_map = list(range(len(embeddings)))
    if world_size == 1:
      self.local_configs = self.global_configs
      self.local_input_table_map = input_table_map
      self.input_ids_list = [list(range(len(input_table_map)))]
      self.table_ids_list = [list(range(len(embeddings)))]
      return

    # Create (maybe) sliced configs
    sliced_configs, self.sliced_out_ranges = self.create_sliced_configs(
        world_size, column_slice_threshold, input_table_map)
    # Apply strategy and save nested list containing table indices by rank
    self.table_ids_list = self.apply_stragety(strategy, world_size, sliced_configs)

    # Nested list containing input indices by rank
    self.input_ids_list = []
    # Nested list containing local input to local table map by rank
    self.local_map_list = []
    # Nested list containing local configs by rank
    self.local_configs_list = []
    # All of local widths ordered by rank flat into single list
    self.widths_list_flat = []
    # Each worker loop over all rank to get global view of strategy
    for rank_table_ids in self.table_ids_list:
      # calculate stats needed for each rank
      rank_widths, rank_input_ids, rank_input_map, rank_configs = [], [], [], []
      for m, table_idx in enumerate(rank_table_ids):
        rank_configs.append(sliced_configs[table_idx].pop(0))
        for k, mapped_idx in enumerate(input_table_map):
          if table_idx == mapped_idx:
            rank_widths.append(rank_configs[-1]['output_dim'])
            rank_input_ids.append(k)
            rank_input_map.append(m)
      self.local_configs_list.append(rank_configs)
      self.widths_list_flat += rank_widths
      self.input_ids_list.append(rank_input_ids)
      self.local_map_list.append(rank_input_map)

    # List that maps local inputs to local table
    self.local_input_table_map = self.local_map_list[rank]

    # flatten self.input_ids_list
    worker_order_input_ids = [item for sublist in self.input_ids_list for item in sublist]

    # List of indices to shuffle worker ordered embedding outputs back to original order
    self.rev_global_input_ids = [
        index
        for _, index in sorted(zip(worker_order_input_ids, range(len(worker_order_input_ids))))
    ]

    # List of configs to create local embedding layers
    self.local_configs = self.local_configs_list[rank]

  def maybe_slice_table_column(self, orig_config, column_slice_threshold, world_size):
    """Column slice a embedding config if size exceed column_slice_threshold.
    Assume N is smallest power of 2 so that when evenly slice original table into N tables,
    each have less than column_slice_threshold elements.
    So final number of slices will be min(N, world_size, table_width).
    Args:
      orig_config (dict): embedding layer config to create slices from
      column_slice_threshold (int or None): desired upper bound of elements count in each slice
      world_size (int): number of total model parallel worker
    Returns:
      sliced_config (list): list of embedding layer config that concat into original config
    """
    if column_slice_threshold is None:
      column_slice_threshold = float('inf')
    table_size = orig_config['input_dim'] * orig_config['output_dim']
    num_slices = 1
    while table_size > column_slice_threshold:
      num_slices *= 2
      table_size /= 2
    if num_slices == 1:
      return [orig_config.copy()]
    num_slices = min(num_slices, world_size, orig_config['output_dim'])
    column_per_slice = orig_config['output_dim'] // num_slices
    remainder = orig_config['output_dim'] % num_slices
    sliced_config = []
    for i in range(num_slices):
      config = orig_config.copy()
      config['output_dim'] = column_per_slice
      if i < remainder:
        config['output_dim'] += 1
      sliced_config.append(config)
    return sliced_config

  def create_sliced_configs(self, world_size, column_slice_threshold, input_table_map):
    """Create column sliced configs from global configs.
    This function also calculate ranges of data parallel output needs concat due to this slice.
    Args:
      world_size (int): number of model parallel workers
      column_slice_threshold (int or None): desired upper bound of elements count in each slice
      input_table_map (list): A list of table ids mapping each input to a table
    Returns:
      sliced_configs (list): same length as global configs. each element is a list represent sliced
    form of global config at the same position.
      sliced_out_ranges (list): each element is list of 2 integers, representing output ranges need
    to be concatenated to re-form output due to above slice.
    """
    sliced_configs = []
    for global_config in self.global_configs:
      maybe_sliced_config = self.maybe_slice_table_column(global_config, column_slice_threshold,
                                                          world_size)
      sliced_configs.append(maybe_sliced_config)
    # figure out ranges of output that needs concat
    # this needs to be in output order, otherwise range modification would fail
    sliced_out_ranges = []
    for input_id, table_id in enumerate(input_table_map):
      if len(sliced_configs[table_id]) > 1:
        sliced_out_ranges.append([input_id, input_id + len(sliced_configs[table_id])])
    return sliced_configs, sliced_out_ranges

  # pylint: disable=missing-param-doc,missing-type-doc,missing-raises-doc
  def apply_stragety(self, mode, world_size, sliced_configs):
    """Distribute tables to workers from sliced config, a nested list.
    Returns:
      divided_ids (list): world_size length list. Each element is list of
    sliced table ids distribute to rank according to position.
    """
    global_ids = []
    table_sizes = []
    for i, sliced_config in enumerate(sliced_configs):
      for config in sliced_config:
        global_ids.append(i)
        table_sizes.append(config['input_dim'] * config['output_dim'])

    # Round-robin distribute tables onto workers
    if mode == 'basic':
      divided_ids = [global_ids[i::world_size] for i in range(world_size)]
    # Distributed table so that memory is balanced while table count remain even
    elif mode == 'memory_balanced':
      sorted_ids = [idx for _, idx in sorted(zip(table_sizes, global_ids), reverse=True)]
      divided_ids = [
          sorted_ids[i::2 * world_size] + sorted_ids[(2 * world_size - 1 - i)::2 * world_size]
          for i in range(world_size)
      ]
    # Try to optimize for total memory first. After sorted by size, table are distributed one by one
    # to worker with lowest total size. Memory usage will be more even but table count may not.
    elif mode == 'memory_optimized':
      sorted_pairs = list(sorted(zip(table_sizes, global_ids)))
      res = [[0, []] for _ in range(world_size)]
      while sorted_pairs:
        cur = sorted_pairs.pop()
        res[0][0] += cur[0]
        res[0][1].append(cur[1])
        res = sorted(res)
      divided_ids = [r[1] for r in res]
    else:
      raise ValueError(F"Unsupported strategy {strategy}")
    return divided_ids


class DistributedEmbedding(tf.keras.layers.Layer):
  """Distributed embedding wrapper

  This class is a hybrid parallel wrapper around embedding. It handles all to all communication of
  forward and backward of embedding.

  Args:
    embeddings (list of keras Embedding layers): embedding tables to be distributed
    strategy (str): A string indicates how embedding tables are distributed.
        Choices are [“basic”, “memory_balanced”]. Default "basic"
    column_slice_threshold (int or None): If not None, embedding tables with more elements than
        column_slice_threshold will be divide into N even pieces alone embedded width dimension.
        N is smallest power of 2 makes each slice smaller than column_slice_threshold. Default None.
    row_slice (TBD): Describe how which embedding needs to be row sliced
    dp_input (bool): If True, takes data parallel input, i.e. in shape
        [local_batch_size x global_num_embeddings]. Otherwise take model parall input in shape
        [global_batch_size x local_num_embeddings]. Default True.
    input_table_map (list or None): same length list as inputs, map `input[i]`
        to `table[input_table_map[i]]`. None means there are same number of
        inputs/tables and `input[i]` map to `table[i]`. Default None.
  """

  def __init__(self,
               embeddings,
               strategy="basic",
               column_slice_threshold=None,
               row_slice=None,
               dp_input=True,
               input_table_map=None,
               **kwargs):

    super().__init__(**kwargs)
    if strategy not in ['basic', 'memory_balanced', 'memory_optimized']:
      raise ValueError(F"Unsupported shard strategy {strategy}")
    if row_slice is not None:
      raise NotImplementedError("Row slicing embedding is not supported yet!")

    # Currently assume data parallel ranks == model parallel ranks
    # TODO(Deyu): add more control over this with newly added hvd process_set api
    if not hvd.is_initialized():
      hvd.init()
    self.world_size = hvd.size()
    self.rank = hvd.rank()

    self.dp_input = dp_input
    self.column_slice_threshold = column_slice_threshold
    # get model parallel distribution strategy
    self.strategy = DistEmbeddingStrategy(embeddings,
                                          self.world_size,
                                          self.rank,
                                          strategy,
                                          input_table_map=input_table_map,
                                          column_slice_threshold=column_slice_threshold)
    if len(self.strategy.global_configs) < self.world_size:
      raise NotImplementedError

    # create local embeddings
    self.local_embedding_layers = []
    for config in self.strategy.local_configs:
      config['synchronization'] = tf.VariableSynchronization.NONE
      self.local_embedding_layers.append(Embedding.from_config(config))

  def _call_base(self, inputs):  # pylint: disable=missing-param-doc,missing-type-doc
    """Call function that do embeddings and communication

    Currently, it requires same batch_size on all workers.
    """
    # get model parallel input from data parallel
    if self.dp_input:
      comm_dtype = tf.int32
      for inp in inputs:
        if inp.dtype == tf.int64:
          comm_dtype = tf.int64
      inputs = [tf.cast(inp, comm_dtype) for inp in inputs]
      local_shapes, local_splits, global_splits, flat_inputs = [], [], [], []
      for rank_input_ids in self.strategy.input_ids_list:
        rank_inputs = [inputs[index] for index in rank_input_ids]
        local_shapes.append([inp.shape for inp in rank_inputs])
        rank_inputs = [tf.reshape(inp, [-1]) for inp in rank_inputs]
        local_splits.append([inp.shape[0] for inp in rank_inputs])
        global_splits.append(sum(local_splits[-1]))
        flat_inputs += rank_inputs
      inputs = tf.concat(flat_inputs, 0)
      inputs, _ = hvd.alltoall(inputs, splits=global_splits, name='inp_dp_to_mp')
      inputs = tf.reshape(inputs, [self.world_size, -1])
      inputs = tf.split(inputs, local_splits[self.rank], 1)
      inputs = [
          tf.reshape(inp, [self.world_size * shape[0]] + shape[1:])
          for inp, shape in zip(inputs, local_shapes[self.rank])
      ]

    # do embedding
    mp_outs = [
        self.local_embedding_layers[m](inp)
        for m, inp in zip(self.strategy.local_input_table_map, inputs)
    ]

    # TODO(Deyu): current assume 2D with same batch for all output, ideally should support general case
    mp_outs = [tf.reshape(mp_out, [self.world_size, -1]) for mp_out in mp_outs]
    mp_outs = tf.reshape(tf.concat(mp_outs, axis=1), [-1])
    # cast before alltoall according to dtype policy
    mp_outs = tf.cast(mp_outs, self.compute_dtype)
    dp_outs = hvd.alltoall(mp_outs, name='out_mp_to_dp')
    batch_size = tf.shape(
        inputs[0], out_type=tf.int32)[0] if inputs[0].shape[0] is None else inputs[0].shape[0]
    local_bs = batch_size // self.world_size
    num_elements = [local_bs * item for item in self.strategy.widths_list_flat]
    split_outs = tf.split(dp_outs, num_elements)
    worker_order_res = [tf.reshape(split_out, [local_bs, -1]) for split_out in split_outs]

    # reorder outputs to be same as inputs order
    result = [worker_order_res[index] for index in self.strategy.rev_global_input_ids]
    return result

  def _concat_column_slice_outputs(self, outs):
    """Concat sliced outputs result from column slicing back together"""
    for start, end in self.strategy.sliced_out_ranges:
      outs[start:end] = [tf.concat(outs[start:end], axis=-1)]
    return outs

  def set_weights(self, weights, chunk=134217728, use_lock=False):
    """Sets the weights of the layer, from NumPy arrays.

    Args:
      weights (list): list containing global weights for all table.
          item in the list can be either numpy array or file path to load from.
      chunk (int): max number of elements per chunk when set weight on GPU by chunks.
          this will be round to number of rows base on weight shape.
      use_lock (bool): If true, set weights rank by rank in lock step to avoid OOM. Default False.
    """
    if use_lock:
      for _ in range(self.rank):
        hvd.broadcast_object(0)

    if self.world_size > 1:
      slice_info = [[rank_tids.count(tid)
                     for rank_tids in self.strategy.table_ids_list]
                    for tid in range(len(weights))]
      weights = [weights[index] for index in self.strategy.table_ids_list[self.rank]]
      if isinstance(weights[0], str):
        weights = [np.load(file=path, mmap_mode='r') for path in weights]
      local_info = [slice_info[index] for index in self.strategy.table_ids_list[self.rank]]
      # array to handle multiple slice into same table case
      # TODO(Deyu): avoid this by merge those table again after find strategy
      rank_ids = self.strategy.table_ids_list[self.rank]
      index_offset = [rank_ids[:i].count(rank_id) for i, rank_id in enumerate(rank_ids)]

      def _slice_weight_for_rank(weight, info, global_rank, offset):
        num_columns = weight.shape[1]
        num_slices = sum(info)
        column_per_slice = num_columns // num_slices
        remainder = num_columns % num_slices
        rank = sum(info[:global_rank]) + offset

        start = column_per_slice * rank + min(rank, remainder)
        rank += 1
        end = column_per_slice * rank + min(rank, remainder)
        return weight[:, start:end]

      weights = [
          _slice_weight_for_rank(weight, info, self.rank, offset)
          for weight, info, offset in zip(weights, local_info, index_offset)
      ]
    # variable.assign and copy-on-write creates extra copy of weight that causes OOM
    # so here we scatter update by ~128M elements chunks instead of just do
    # super().set_weights(weights)
    for weight, arr in zip(self.weights, weights):
      if arr.size <= chunk:
        weight.assign(arr)
      else:
        chunk_size_dim0 = chunk // weight.shape[1]
        num_chunks = math.ceil(weight.shape[0] / chunk_size_dim0)
        last_size = weight.shape[0] - chunk_size_dim0 * (num_chunks - 1)
        chunk_sizes = [chunk_size_dim0] * (num_chunks - 1) + [last_size]
        for i in range(num_chunks):
          start = i * chunk_size_dim0
          end = start + chunk_sizes[i]
          indices = tf.range(start=start, limit=end, dtype=tf.int64)
          update = tf.IndexedSlices(values=arr[start:end],
                                    indices=indices,
                                    dense_shape=weight.shape)
          weight.scatter_update(sparse_delta=update)
    del weights

    if use_lock:
      for _ in range(self.world_size - self.rank):
        hvd.broadcast_object(0)

  # 1d split that works beyond 32bit indexing limit TF support
  def _split_1d(self, tensor, lengths):
    # choose a number close to int32 limit as maximum chunk size
    # This will handle tensor with size up to square of int32_max
    chunking_threshold = 2147483646
    if tensor.shape[0] <= chunking_threshold:
      return tf.split(tensor, lengths)
    num_chunks = math.ceil(tensor.shape[0] / chunking_threshold)
    padding_len = math.ceil(tensor.shape[0] / num_chunks) * num_chunks - tensor.shape[0]
    padded_tensor = tf.concat([tensor, tf.zeros(padding_len, tensor.dtype)], axis=0)
    tensor_list = tf.unstack(tf.reshape(padded_tensor, [num_chunks, -1]))
    result = []
    for length in lengths:
      this_slice = []
      while length > 0:
        if length > tensor_list[0].shape[0]:
          this_slice.append(tensor_list.pop(0))
        else:
          this_slice.append(tensor_list[0][:length])
          tensor_list[0] = tensor_list[0][length:]
        length -= this_slice[-1].shape[0]
      result.append(tf.concat(this_slice, axis=0))
    return result

  def get_weights(self, all_ranks=False):
    """Returns the current weights of the layer, as NumPy arrays.

    This override outputs global weights for all tables.
    Args:
      all_ranks (bool): If true, return weights in all ranks, otherwise only in rank 0.
          Default False.
    Returns:
      result (list): List of weight tensors.
    """
    # avoid copy-on-read on dense access
    local_weights = [read_var_no_copy(w) for w in self.weights]
    if self.world_size == 1:
      return [w.numpy() for w in local_weights]

    # mpi segfault on over 32bit range index, so we gather weights chunk by chunk here
    # choose a number not very close to int32 limit as maximum chunk size just to be safe
    chunking_threshold = 2000000000
    num_chunks = 1
    for local_configs in self.strategy.local_configs_list:
      total_elements = sum([c['input_dim'] * c['output_dim'] for c in local_configs])
      num_chunks = max(num_chunks, math.ceil(self.world_size * total_elements / chunking_threshold))

    with tf.device('CPU:0'):
      local_weights = tf.concat([tf.reshape(w, [-1]) for w in local_weights], axis=0)
      chunk_size = local_weights.shape[0] // num_chunks
      last_size = local_weights.shape[0] - chunk_size * (num_chunks - 1)
      chunk_sizes = [chunk_size] * (num_chunks - 1) + [last_size]
      local_weights = self._split_1d(local_weights, chunk_sizes)
      # communicate chunk sizes
      all_sizes = hvd.allgather(chunk_sizes)

      # collect all chunks and split to reverse allgather concat
      chunks = []
      for i, w in enumerate(local_weights):
        w = hvd.allgather(w)
        if all_ranks or self.rank == 0:
          chunks += self._split_1d(w, all_sizes[i::num_chunks])
      if not chunks:
        return []

      # re-construct all local weights from chunks
      local_weights = []
      for i in range(self.world_size):
        local_weights.append(tf.concat(chunks[i::self.world_size], axis=0))
      del chunks

      # split flat local weights into correct sizes
      weights = []
      for local_weight, local_configs in zip(local_weights, self.strategy.local_configs_list):
        local_shapes = [[c['input_dim'], c['output_dim']] for c in local_configs]
        local_sizes = [shape[0] * shape[1] for shape in local_shapes]
        flat_weights = self._split_1d(local_weight, local_sizes)
        weights += [tf.reshape(weight, shape) for weight, shape in zip(flat_weights, local_shapes)]
      # restore original table order
      # flatten self.strategy.table_ids_list
      worker_order_table_ids = [
          item for sublist in self.strategy.table_ids_list for item in sublist
      ]
      # Shuffle worker ordered embedding weights(sliced) back to original order.
      ids_and_weights = sorted(zip(worker_order_table_ids, weights), key=lambda x: x[0])
      # concat sliced weights
      result = []
      cur_id = 0
      cur_list = []
      while ids_and_weights:
        cur = ids_and_weights.pop(0)
        if cur[0] == cur_id:
          cur_list.append(cur[1])
        else:
          result.append(tf.concat(cur_list, axis=1).numpy())
          cur_id = cur[0]
          cur_list = [cur[1]]
      result.append(tf.concat(cur_list, axis=1).numpy())
      return result

  @tf_utils.shape_type_conversion
  def build(self, input_shape):
    for layer in self.local_embedding_layers:
      layer.build(input_shape)
    self.built = True

  def call(self, inputs):  # pylint: disable=missing-function-docstring
    if self.world_size == 1:
      outputs = [
          self.local_embedding_layers[m](inp)
          for m, inp in zip(self.strategy.local_input_table_map, inputs)
      ]
      outputs = [tf.cast(output, self.compute_dtype) for output in outputs]
      return outputs

    # TODO(skyw): Revisit logics of selecting call functions for different strategy
    outputs = self._call_base(inputs)
    outputs = self._concat_column_slice_outputs(outputs)
    return outputs


# Monkey patch horovod bcast/tape so we can handle mp/dp vars differently in single backward
def broadcast_variables(model_vars, root_rank=0):  # pylint: disable=missing-any-param-doc
  """Broadcasts variables from root rank to all other processes in a process set

  Replace horovod's broadcast_variables when running hybrid parallel

  See https://horovod.readthedocs.io/en/stable/api.html for more details
  """
  dp_vars = []
  mp_vars = []
  for var in model_vars:
    if var.synchronization == tf.VariableSynchronization.NONE:
      mp_vars.append(var)
    else:
      dp_vars.append(var)
  hvd.broadcast_variables(dp_vars, root_rank=root_rank)


def DistributedGradientTape(*args, **kwargs):  # pylint: disable=missing-param-doc,invalid-name
  """Graident tape that supports hybrid parallel

  Replace horovod's DistributedGradientTape when running hybrid parallel

  See https://horovod.readthedocs.io/en/stable/api.html for more details
  """

  def gradient(self, target, sources, output_gradients=None):
    gradients = super(self.__class__, self).gradient(target, sources, output_gradients)
    dp_vars = []
    dp_grads = []
    mp_grads = []
    split_infos = []
    for grad, var in zip(gradients, sources):
      if var.synchronization == tf.VariableSynchronization.NONE:
        if isinstance(grad, tf.IndexedSlices):
          mp_grads.append(tf.IndexedSlices(grad.values / hvd.size(), grad.indices,
                                           grad.dense_shape))
        else:
          mp_grads.append(grad / hvd.size())
        split_infos.append((True, len(mp_grads) - 1))
      else:
        dp_vars.append(var)
        dp_grads.append(grad)
        split_infos.append((False, len(dp_grads) - 1))
    # TODO(Deyu): make sure not reusing _allreduce_grads doesn't lead to any issue
    dp_grads = [
        hvd.allreduce(g, name=f'dp_gradient_{i}', op=hvd.Average) for i, g in enumerate(dp_grads)
    ]
    # put gradients back in original order
    grads = []
    for info in split_infos:
      if info[0]:
        grads.append(mp_grads[info[1]])
      else:
        grads.append(dp_grads[info[1]])
    return grads

  tape = hvd.DistributedGradientTape(*args, **kwargs)
  setattr(type(tape), 'gradient', gradient)
  return tape
