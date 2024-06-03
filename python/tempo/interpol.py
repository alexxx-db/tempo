from typing import Union, Callable
import copy

import pandas as pd

import pyspark.sql.functions as sfn
from pyspark.sql import Window

from tempo.tsdf import TSDF

# Some common interpolation functions


def zero_fill(null_series: pd.Series) -> pd.Series:
    return null_series.fillna(0)


def forward_fill(null_series: pd.Series) -> pd.Series:
    return null_series.ffill()


def backward_fill(null_series: pd.Series) -> pd.Series:
    return null_series.bfill()


# The interpolation

def _build_interpolator(
        interpol_col: str,
        interpol_fn: Callable[[pd.Series], pd.Series]
) -> Callable[[pd.DataFrame], pd.DataFrame]:
    def interpolator_fn(pdf: pd.DataFrame) -> pd.DataFrame:
        # those rows that need interpolation
        na_mask = pdf[interpol_col].isna()
        # otherwise we interpolate the missing values
        pdf[interpol_col] = interpol_fn(pdf[interpol_col])
        # return only the rows that were missing (others are margins)
        return pdf[na_mask]

    return interpolator_fn


def interpolate(
    tsdf: TSDF,
    col: str,
    fn: Union[Callable[[pd.Series], pd.Series], str],
    leading_margin: int = 1,
    lagging_margin: int = 0
) -> TSDF:
    """
    Interpolate missing values in a time series column.

    For the given column, null values are assumed to be missing, and
    this method will attempt to interpolate them using the given function.
    The interpolation function can be a string representing a valid method
    for the pandas Series.interpolate method, or a custom function that takes
    a pandas Series and returns a pandas Series of the same length.
    The Series given may include a "margin" of
    leading or trailing non-missing (i.e. non-null) values to help the
    interpolation function. The exact size of the leading and trailing margins are
    configurable. The function should not attempt to modify these margin values.

    :param tsdf: the :class:`TSDF` timeseries dataframe
    :param col: the name of the column to interpolate
    :param fn: the interpolation function
    :param leading_margin: the number of non-missing values
    to include before the first missing value
    :param lagging_margin: the number of non-missing values
    to include after the last missing value

    :return: a new :class:`TSDF` with the missing values of the given
    column interpolated
    """

    # identify transitions between segments
    seg_trans_col = "__tmp_seg_transition"
    all_win = tsdf.baseWindow()
    segments = tsdf.df.withColumn(seg_trans_col,
                                  sfn.lag(col, 1).over(all_win).isNull()
                                  != sfn.col(col).isNull())

    # assign a group number to each segment
    seg_group_col = "__tmp_seg_group"
    all_prev_win = tsdf.allBeforeWindow()
    segments = segments.withColumn(seg_group_col,
                                   sfn.count_if(seg_trans_col).over(all_prev_win))

    # build margins around intepolation segments
    if leading_margin > 0 or lagging_margin > 0:
        # collect the group number of each segment with a margin
        margin_col = "__tmp_group_with_margin"
        margin_win = tsdf.rowsBetweenWindow(-leading_margin, lagging_margin)
        segments = segments.withColumn(margin_col,
                                       sfn.when(sfn.col(col).isNotNull(),
                                                sfn.collect_set(seg_group_col)
                                                .over(margin_win))
                                       .otherwise(sfn.array(seg_group_col)))
        # explode the groups with margins
        needs_intpl_col = "__tmp_needs_interpol"
        explode_exprs = tsdf.df.columns + [sfn.explode(margin_col).alias(seg_group_col)]

    # identify segments that need interpolation
    segment_win = Window.partitionBy("symbol", "gap_group")
    segments = (segments.select(*explode_exprs)
                .withColumn(needs_intpl_col,
                            sfn.bool_or(sfn.col(col).isNull()).over(segment_win)))

    # split the segments according to the need for interpolation
    needs_interpol = segments.where(needs_intpl_col)
    no_interpol = segments.where(~sfn.col(needs_intpl_col))

    # build the interpolator function
    if isinstance(fn, str):
        interpolator = _build_interpolator(col, lambda x: x.interpolate(method=fn))
    else:
        interpolator = _build_interpolator(col, fn)

    # apply the interpolator to each segment
    interpolated_df = needs_interpol.groupBy(tsdf.series_ids + [seg_group_col]) \
        .applyInPandas(interpolator, needs_interpol.schema)

    # merge the interpolated segments with the non-interpolated ones
    final_df = (no_interpol.union(interpolated_df)
                .drop(seg_group_col, needs_intpl_col))

    # return it as a new TSDF
    return TSDF(final_df, ts_schema=copy.deepcopy(tsdf.ts_schema))
