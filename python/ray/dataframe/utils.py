from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import pandas
import numpy as np
import ray

from . import get_npartitions


_NAN_BLOCKS = {}
_MEMOIZER_CAPACITY = 1000  # Capacity per function


class LRUCache:
    """A LRUCache implemented with collections.OrderedDict

    Notes:
        - OrderedDict will record the order each item is inserted.
        - The head of the queue will be LRU items.
    """

    def __init__(self, capacity):
        self.capacity = capacity
        self.cache = collections.OrderedDict()

    def __contains__(self, key):
        return key in self.cache

    def __getitem__(self, key):
        """Retrieve item from cache and re-insert it to the back of the queue
        """
        value = self.cache.pop(key)
        self.cache[key] = value
        return value

    def __setitem__(self, key, value):
        if key in self.cache:
            self.cache.pop(key)

        if len(self.cache) >= self.capacity:
            # Pop oldest items at the beginning of the queue
            self.cache.popitem(last=False)

        self.cache[key] = value


class memoize:
    """A basic memoizer that cache the input and output of the remote function

    Notes:
        - How is this implemented?
          This meoizer is implemented by adding a caching layer to the remote
          function's remote attribute. When user call f.remote(*args), we will
          first check against the cache, and then call the ray remote function
          if we can't find the return value in the cache.
        - When should this be used?
          This should be used when we anticipate temporal locality for the
          function. For example, we can reasonally assume users will perform
          columnar operation repetitively over time (like sum() or loc[]).
        - Caveat
          Don't use this decorator if the any argument to the remote function
          will mutate. Following snippet will fail
          ```py
              @memoize
              @ray.remote
              def f(obj):
                ...

              mutable_obj = [1]
              oid_1 = f.remote(mutable_obj) # will be cached

              mutable_obj.append(3)
              oid_2 = f.remote(mutable_obj) # cache hit!

              oid_1 == oid_2 # True!
           ```
           In short, use this function sparingly. The ideal case is that all
           inputs are ray ObjectIDs because they are immutable objects.
        - Future Development
          - Fix the mutability bug
          - Dynamic cache size (Fixed as 1000 for now)
    """

    def __init__(self, f):
        # Save of remote function
        self.old_remote_func = f.remote
        self.cache = LRUCache(capacity=_MEMOIZER_CAPACITY)

    def remote(self, *args):
        """Return cached result if the arguments are cached
        """
        args = tuple(args)

        if args in self.cache:
            cached_result = self.cache[args]
            return cached_result

        result = self.old_remote_func(*args)
        self.cache[args] = result
        return result


def _get_nan_block_id(n_row=1, n_col=1, transpose=False):
    """A memory efficent way to get a block of NaNs.

    Args:
        n_rows(int): number of rows
        n_col(int): number of columns
        transpose(bool): if true, swap rows and columns
    Returns:
        ObjectID of the NaN block
    """
    global _NAN_BLOCKS
    if transpose:
        n_row, n_col = n_col, n_row
    shape = (n_row, n_col)
    if shape not in _NAN_BLOCKS:
        arr = np.tile(np.array(np.NaN), shape)
        _NAN_BLOCKS[shape] = ray.put(pandas.DataFrame(data=arr))
    return _NAN_BLOCKS[shape]


def _get_lengths(df):
    """Gets the length of the dataframe.
    Args:
        df: A remote pandas.DataFrame object.
    Returns:
        Returns an integer length of the dataframe object. If the attempt
            fails, returns 0 as the length.
    """
    try:
        return len(df)
    # Because we sometimes have cases where we have summary statistics in our
    # DataFrames
    except TypeError:
        return 0


def _get_widths(df):
    """Gets the width (number of columns) of the dataframe.
    Args:
        df: A remote pandas.DataFrame object.
    Returns:
        Returns an integer width of the dataframe object. If the attempt
            fails, returns 0 as the length.
    """
    try:
        return len(df.columns)
    # Because we sometimes have cases where we have summary statistics in our
    # DataFrames
    except TypeError:
        return 0


def _partition_pandas_dataframe(df, num_partitions=None, row_chunksize=None):
    """Partitions a Pandas DataFrame object.
    Args:
        df (pandas.DataFrame): The pandas DataFrame to convert.
        npartitions (int): The number of partitions to split the DataFrame
            into. Has priority over chunksize.
        row_chunksize (int): The number of rows to put in each partition.
    Returns:
        [ObjectID]: A list of object IDs corresponding to the dataframe
        partitions
    """
    if num_partitions is not None:
        row_chunksize = len(df) // num_partitions \
            if len(df) % num_partitions == 0 \
            else len(df) // num_partitions + 1
    else:
        assert row_chunksize is not None

    temp_df = df

    row_partitions = []
    while len(temp_df) > row_chunksize:
        t_df = temp_df[:row_chunksize]
        # reset_index here because we want a pandas.RangeIndex
        # within the partitions. It is smaller and sometimes faster.
        t_df.reset_index(drop=True, inplace=True)
        t_df.columns = pandas.RangeIndex(0, len(t_df.columns))
        top = ray.put(t_df)
        row_partitions.append(top)
        temp_df = temp_df[row_chunksize:]
    else:
        # Handle the last chunk correctly.
        # This call is necessary to prevent modifying original df
        temp_df = temp_df[:]
        temp_df.reset_index(drop=True, inplace=True)
        temp_df.columns = pandas.RangeIndex(0, len(temp_df.columns))
        row_partitions.append(ray.put(temp_df))

    return row_partitions


def from_pandas(df, num_partitions=None, chunksize=None):
    """Converts a pandas DataFrame to a Ray DataFrame.
    Args:
        df (pandas.DataFrame): The pandas DataFrame to convert.
        num_partitions (int): The number of partitions to split the DataFrame
            into. Has priority over chunksize.
        chunksize (int): The number of rows to put in each partition.
    Returns:
        A new Ray DataFrame object.
    """
    from .dataframe import DataFrame

    row_partitions = \
        _partition_pandas_dataframe(df, num_partitions, chunksize)

    return DataFrame(row_partitions=row_partitions,
                     columns=df.columns,
                     index=df.index)


def to_pandas(df):
    """Converts a Ray DataFrame to a pandas DataFrame/Series.
    Args:
        df (ray.DataFrame): The Ray DataFrame to convert.
    Returns:
        A new pandas DataFrame.
    """
    pandas_df = pandas.concat(ray.get(df._row_partitions), copy=False)
    pandas_df.index = df.index
    pandas_df.columns = df.columns
    return pandas_df


@ray.remote
def extractor(df_chunk, row_loc, col_loc):
    """Retrieve an item from remote block
    """
    # We currently have to do the writable flag trick because a pandas bug
    # https://github.com/pandas-dev/pandas/issues/17192
    try:
        row_loc.flags.writeable = True
        col_loc.flags.writeable = True
    except AttributeError:
        # Locators might be scaler or python list
        pass
    return df_chunk.iloc[row_loc, col_loc]


@ray.remote
def writer(df_chunk, row_loc, col_loc, item):
    """Make a copy of the block and write new item to it
    """
    df_chunk = df_chunk.copy()
    df_chunk.iloc[row_loc, col_loc] = item
    return df_chunk


def _mask_block_partitions(blk_partitions, row_metadata, col_metadata):
    """Return the squeezed/expanded block partitions as defined by
    row_metadata and col_metadata.

    Note:
        Very naive implementation. Extract one scaler at a time in a double
        for loop.
    """
    col_df = col_metadata._coord_df
    row_df = row_metadata._coord_df

    result_oids = []
    shape = (len(row_df.index), len(col_df.index))

    for _, row_partition_data in row_df.iterrows():
        for _, col_partition_data in col_df.iterrows():
            row_part = row_partition_data.partition
            col_part = col_partition_data.partition
            block_oid = blk_partitions[row_part, col_part]

            row_idx = row_partition_data['index_within_partition']
            col_idx = col_partition_data['index_within_partition']

            result_oid = extractor.remote(block_oid, [row_idx], [col_idx])
            result_oids.append(result_oid)
    return np.array(result_oids).reshape(shape)


@ray.remote
def _deploy_func(func, dataframe, *args):
    """Deploys a function for the _map_partitions call.
    Args:
        dataframe (pandas.DataFrame): The pandas DataFrame for this partition.
    Returns:
        A futures object representing the return value of the function
        provided.
    """
    if len(args) == 0:
        return func(dataframe)
    else:
        return func(dataframe, *args)


def _map_partitions(func, partitions, *argslists):
    """Apply a function across the specified axis

    Args:
        func (callable): The function to apply
        partitions ([ObjectID]): The list of partitions to map func on.

    Returns:
        A list of partitions ([ObjectID]) with the result of the function
    """
    if partitions is None:
        return None

    assert(callable(func))
    if len(argslists) == 0:
        return [_deploy_func.remote(func, part) for part in partitions]
    elif len(argslists) == 1:
        return [_deploy_func.remote(func, part, argslists[0])
                for part in partitions]
    else:
        assert(all(len(args) == len(partitions) for args in argslists))
        return [_deploy_func.remote(func, *args)
                for args in zip(partitions, *argslists)]


@ray.remote
def _build_col_widths(df_col):
    """Compute widths (# of columns) for each partition."""
    widths = np.array(ray.get([_deploy_func.remote(_get_widths, d)
                      for d in df_col]))

    return widths


@ray.remote
def _build_row_lengths(df_row):
    """Compute lengths (# of rows) for each partition."""
    lengths = np.array(ray.get([_deploy_func.remote(_get_lengths, d)
                       for d in df_row]))

    return lengths


@ray.remote
def _build_coord_df(lengths, index):
    """Build the coordinate dataframe over all partitions."""
    filtered_lengths = [x for x in lengths if x > 0]
    coords = None
    if len(filtered_lengths) > 0:
        coords = np.vstack([np.column_stack((np.full(l, i), np.arange(l)))
                            for i, l in enumerate(filtered_lengths)])
    col_names = ("partition", "index_within_partition")
    return pandas.DataFrame(coords, index=index, columns=col_names)


def _create_block_partitions(partitions, axis=0, length=None):

    if length is not None and length != 0 and get_npartitions() > length:
        npartitions = length
    elif length == 0:
        npartitions = 1
    else:
        npartitions = get_npartitions()

    x = [create_blocks._submit(args=(partition, npartitions, axis),
                               num_return_vals=npartitions)
         for partition in partitions]

    # In the case that axis is 1 we have to transpose because we build the
    # columns into rows. Fortunately numpy is efficient at this.
    blocks = np.array(x) if axis == 0 else np.array(x).T

    # Sometimes we only get a single column or row, which is
    # problematic for building blocks from the partitions, so we
    # add whatever dimension we're missing from the input.
    return fix_blocks_dimensions(blocks, axis)


@ray.remote
def create_blocks(df, npartitions, axis):
    return create_blocks_helper(df, npartitions, axis)


def create_blocks_helper(df, npartitions, axis):
    # Single partition dataframes don't need to be repartitioned
    if npartitions == 1:
        return df
    # In the case that the size is not a multiple of the number of partitions,
    # we need to add one to each partition to avoid losing data off the end
    block_size = df.shape[axis ^ 1] // npartitions \
        if df.shape[axis ^ 1] % npartitions == 0 \
        else df.shape[axis ^ 1] // npartitions + 1

    # if not isinstance(df.columns, pandas.RangeIndex):
    #     df.columns = pandas.RangeIndex(0, len(df.columns))

    blocks = [df.iloc[:, i * block_size: (i + 1) * block_size]
              if axis == 0
              else df.iloc[i * block_size: (i + 1) * block_size, :]
              for i in range(npartitions)]

    for block in blocks:
        block.columns = pandas.RangeIndex(0, len(block.columns))
        block.reset_index(inplace=True, drop=True)
    return blocks


@memoize
@ray.remote
def _blocks_to_col(*partition):
    if len(partition):
        return pandas.concat(partition, axis=0, copy=False)\
            .reset_index(drop=True)
    else:
        return pandas.Series()


@memoize
@ray.remote
def _blocks_to_row(*partition):
    row_part = pandas.concat(partition, axis=1, copy=False)\
        .reset_index(drop=True)
    # Because our block partitions contain different indices (for the
    # columns), this change is needed to ensure correctness.
    row_part.columns = pandas.RangeIndex(0, len(row_part.columns))
    return row_part


def _inherit_docstrings(parent, excluded=[]):
    """Creates a decorator which overwrites a decorated class' __doc__
    attribute with parent's __doc__ attribute. Also overwrites __doc__ of
    methods and properties defined in the class with the __doc__ of matching
    methods and properties in parent.

    Args:
        parent (object): Class from which the decorated class inherits __doc__.
        excluded (list): List of parent objects from which the class does not
            inherit docstrings.

    Returns:
        function: decorator which replaces the decorated class' documentation
            parent's documentation.
    """
    def decorator(cls):
        if parent not in excluded:
            cls.__doc__ = parent.__doc__
        for attr, obj in cls.__dict__.items():
            parent_obj = getattr(parent, attr, None)
            if parent_obj in excluded or \
                    (not callable(parent_obj) and
                     not isinstance(parent_obj, property)):
                continue
            if callable(obj):
                obj.__doc__ = parent_obj.__doc__
            elif isinstance(obj, property) and obj.fget is not None:
                p = property(obj.fget, obj.fset, obj.fdel, parent_obj.__doc__)
                setattr(cls, attr, p)

        return cls

    return decorator


@ray.remote
def _reindex_helper(old_index, new_index, axis, npartitions, *df):
    """Reindexes a dataframe to prepare for join/concat.

    Args:
        df: The DataFrame partition
        old_index: The index/column for this partition.
        new_index: The new index/column to assign.
        axis: Which axis to reindex over.

    Returns:
        A new set of blocks made up of DataFrames.
    """
    df = pandas.concat(df, axis=axis ^ 1)
    if axis == 1:
        df.index = old_index
    elif axis == 0:
        df.columns = old_index

    df = df.reindex(new_index, copy=False, axis=axis ^ 1)
    return create_blocks_helper(df, npartitions, axis)


@ray.remote
def _co_op_helper(func, left_columns, right_columns, left_df_len, left_idx,
                  *zipped):
    """Copartition operation where two DataFrames must have aligned indexes.

    NOTE: This function assumes things are already copartitioned. Requires that
        row partitions are passed in as blocks.

    Args:
        func: The operation to conduct between two DataFrames.
        left_columns: The column names for the left DataFrame.
        right_columns: The column names for the right DataFrame.
        left_df_len: The length of the left. This is used so we can split up
            the zipped partitions.
        zipped: The DataFrame partitions (in blocks).

    Returns:
         A new set of blocks for the partitioned DataFrame.
    """
    left = pandas.concat(zipped[:left_df_len], axis=1, copy=False).copy()
    left.columns = left_columns
    if left_idx is not None:
        left.index = left_idx

    right = pandas.concat(zipped[left_df_len:], axis=1, copy=False).copy()
    right.columns = right_columns

    new_rows = func(left, right)

    new_blocks = create_blocks_helper(new_rows, left_df_len, 0)

    if left_idx is not None:
        new_blocks.append(new_rows.index)

    return new_blocks


@ray.remote
def _match_partitioning(column_partition, lengths, index):
    """Match the number of rows on each partition. Used in df.merge().

    NOTE: This function can cause problems when there are empty column
        partitions.

        The way this function is intended to be used is as follows: Align the
        right partitioning with the left. The left will remain unchanged. Then,
        you are free to perform actions on a per-partition basis with the
        partitioning.

        The index objects must already be identical for this to work correctly.

    Args:
        column_partition: The column partition to change.
        lengths: The lengths of each row partition to match to.
        index: The index index of the column_partition. This is used to push
            down to the inner frame for correctness in the merge.

    Returns:
         A list of blocks created from this column partition.
    """
    partitioned_list = []

    columns = column_partition.columns
    # We set this because this is the only place we can guarantee correct
    # placement. We use it in the case the user wants to join on the index.
    column_partition.index = index
    for length in lengths:
        if len(column_partition) == 0:
            partitioned_list.append(pandas.DataFrame(columns=columns))
            continue

        partitioned_list.append(column_partition.iloc[:length, :])
        column_partition = column_partition.iloc[length:, :]
    return partitioned_list


@ray.remote
def _concat_index(*index_parts):
    return index_parts[0].append(index_parts[1:])


def fix_blocks_dimensions(blocks, axis):
    """Checks that blocks is 2D, and adds a dimension if not.
    """
    if blocks.ndim < 2:
        return np.expand_dims(blocks, axis=axis ^ 1)
    return blocks


@ray.remote
def _compile_remote_dtypes(*column_of_blocks):
    small_dfs = [df.loc[0:0] for df in column_of_blocks]
    return pandas.concat(small_dfs, copy=False).dtypes
