#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Utility classes, functions, and variables used specifically in the analysis and feature extraction process"""

import json
import warnings
from functools import reduce
from typing import Generator

import numpy as np
import pandas as pd
import scipy.stats as stats
import statsmodels.api as sm
from statsmodels.regression.linear_model import RegressionResultsWrapper

from src import utils
from src.detect.detect_utils import OnsetMaker


class FeatureExtractor:
    """Base class for extracting all features from all instruments within one track

    Examples:
        >>> onsets = OnsetMaker(...)
        >>> made = FeatureExtractor(om=onsets, interpolate=True, interpolation_limit=1)
        >>> made.extract_features()
        >>> made.IOI_beats
        dict(piano=IOISummaryStatsExtractor(...))

    Args:
        om (OnsetMaker): finalized `OnsetMaker` class, corresponding to one track in the corpus
        interpolate (bool, optional): whether to interpolate missing onsets, defaults to True
        interpolation_limit (int, optional): depth in quarter notes to interpolate, defaults to 1
        max_order (int, optional): maximum order to create models up to, defaults to 8 (i.e. 8th-order model)

    """
    _feature_attr_names = [
        'IOI_beats', 'IOI_onsets', 'IOI_bpms', 'IOI_beatsdiff', 'IOI_beatsrolling', 'IOI_onsetsrolling', 'BURs',
        'event_density', 'tempo_slope', 'asynchrony', 'phase_correction', 'granger_causality', 'cross_correlation',
        'partial_correlation', 'metadata'
    ]

    def __init__(
            self,
            om: OnsetMaker,
            **kwargs
    ):
        instrs = utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()
        # Set class attributes from input
        self.om = om
        self.item = om.item
        self.df = pd.DataFrame(om.summary_dict)
        self.max_order = kwargs.get('max_order', 8)    # Maximum order to go up to when creating models
        # If we're interpolating missing IOIs (defaults to true)
        if kwargs.get('interpolate', True):
            # We define these variables only when interpolating
            self.num_interpolated: dict = {k: 0 for k in instrs}
            self.interpolation_limit: int = kwargs.get('interpolation_limit', 1)
            # Iterate through all instruments and replace their extracted beat-onsets with interpolated values
            for instr in instrs:
                self.df[instr] = self.interpolate_missing_onsets(self.df[instr], instr=instr)
        for feature in self._feature_attr_names:
            setattr(self, feature, None)

    def interpolate_missing_onsets(
            self,
            onset_arr,
            instr: str = None,
            interpolation_limit: int = None
    ) -> np.array:
        """Interpolate between observed values to fill missing values in an array of onsets.

        If an onset is missing (set to `np.nan`), we can try to fill it in by looking for onsets that have been found
        on either side (i.e. before and after). We can interpolate up to a particular depth, given by
        interpolation_limit, to fill in a given number of consecutive missing values.

        Examples:
            >>> make = FeatureExtractor()
            >>> bea = np.array([0, np.nan, 1.0, 1.5, np.nan, np.nan, 3.0])
            >>> interp = make.interpolate_missing_onsets(bea, interpolation_limit=1)
            >>> print(interp)
            np.array([0 0.5 1.0 1.5 np.nan np.nan 3.0])

            >>> make = FeatureExtractor()
            >>> bea = np.array([0, np.nan, 1.0, 1.5, np.nan, np.nan, 3.0])
            >>> interp = make.interpolate_missing_onsets(bea, interpolation_limit=2)
            >>> print(interp)
            np.array([0 0.5 1.0 1.5 2.0 2.5 3.0])

        Arguments:
            onset_arr (np.array | pd.Series): the array of onsets to inteprolate
            instr (str, optional): the name of the instrument, used to update num_interpolated counter
            interpolation_limit (int, optional): the depth of missing notes to interpolate up to

        Returns:
            np.array: the onset array with missing onsets interpolated, up to a certain depth.

        """
        # If we haven't passed in an interpolation depth, use the default
        if interpolation_limit is None:
            interpolation_limit = self.interpolation_limit
        # If we've passed in a series, convert this to a numpy array
        if not isinstance(onset_arr, np.ndarray):
            onset_arr = onset_arr.to_numpy()

        # Slice the array to get consecutive missing values
        consecutive = lambda data: np.split(data, np.where(np.diff(data) != 1)[0] + 1)
        cons = consecutive(np.argwhere(np.isnan(onset_arr)).flatten())
        # Iterate through our slices of consecutive missing values
        for con in cons:
            if all([
                len(con) <= interpolation_limit,    # If the number of missing values is below our interpolation limit
                0 not in con,    # If one of the missing values isn't our first onset
                len(onset_arr) not in con    # If one of our missing values isn't our last onset
            ]):
                try:
                    # Get the onsets before and after our missing onsets
                    first, last = onset_arr[con[0] - 1], onset_arr[con[-1] + 1]
                except (IndexError, KeyError):
                    # Skip over missing onsets at the start and end of our array
                    pass
                else:
                    # Fill in the missing onsets with a linear space between the start and end of the array
                    onset_arr[con] = np.linspace(first, last, len(con) + 2)[1:-1]
                    # Increase the number of interpolated onsets
                    if instr is not None:
                        self.num_interpolated[instr] += len(con)
        # Return the interpolated array
        return onset_arr

    # noinspection PyAttributeOutsideInit
    @utils.ignore_warning
    def extract_features(self):
        """Central function for extracting all features from all instruments in a track"""
        def roll(cl, **kwargs) -> dict:
            """Create multiple class instances with given `**kwargs` up to `self.max_order`"""
            return [cl(order=num, **kwargs) for num in range(1, self.max_order)]

        def their_instrs(my_instr: str) -> list:
            """Return list of instruments played by our partners"""
            return [i for i in ins if i != my_instr]

        # Get instrument name list
        ins = utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()
        warnings.filterwarnings('ignore')
        # Extract instrument metadata from `self.item` class
        self.metadata = {i: self._extract_instrument_metadata(i) for i in ins}
        # Inter-onset interval feature classes
        self.IOI_beats = {i: IOISummaryStats(self.df[i]) for i in ins}    # Quarter note beats
        self.IOI_onsets = {i: IOISummaryStats(self.om.ons[i]) for i in ins}    # All onsets
        self.IOI_bpms = {i: IOISummaryStats(self.df[i], use_bpms=True) for i in ins}    # BPMs
        self.IOI_beatsdiff = {i: IOISummaryStats(self.df[i].diff()) for i in ins}    # Quarter note diff
        self.IOI_beatsrolling = {
            i: roll(RollingIOISummaryStats, my_onsets=self.df[i], downbeats=self.om.ons['downbeats_manual'])
            for i in ins
        }
        self.IOI_onsetsrolling = {
            i: roll(RollingIOISummaryStats, my_onsets=self.om.ons[i], downbeats=self.om.ons['downbeats_manual'])
            for i in ins
        }
        # Beat-upbeat ratio features
        self.BURs = {i: BeatUpbeatRatio(my_onsets=self.om.ons[i], my_beats=self.df[i]) for i in ins}
        # Tempo slope features
        self.tempo_slope = dict(
            # Tempo slope classes for each individual instrument
            **{i: TempoSlope(my_beats=self.df[i]) for i in ins},
            # Tempo slope class for the beat tracking algorithm
            madmom=TempoSlope(my_beats=self.df['beats']),
            # Tempo slope class for the average position of the ensemble
            group=TempoSlope(my_beats=self.df[ins].mean(axis=1, skipna=True).rename(1))
        )
        # Asynchrony features
        self.asynchrony = {i: Asynchrony(my_beats=self.df[i], their_beats=self.df[their_instrs(i)]) for i in ins}
        # Phase correction model features
        self.phase_correction = {
            i: roll(PhaseCorrection, my_beats=self.df[i], their_beats=self.df[their_instrs(i)]) for i in ins
        }
        self.granger_causality = {
            i: roll(GrangerCausality, my_beats=self.df[i], their_beats=self.df[their_instrs(i)]) for i in ins
        }
        # Correlation features
        self.partial_correlation = {
            i: roll(PartialCorrelation, my_beats=self.df[i], their_beats=self.df[their_instrs(i)]) for i in ins
        }
        self.cross_correlation = {
            i: roll(CrossCorrelation, my_beats=self.df[i], their_beats=self.df[their_instrs(i)]) for i in ins
        }
        # Event density features
        self.event_density = {
            i: {
                num: EventDensity(
                    my_onsets=self.om.ons[i], downbeats=self.om.ons['downbeats_manual'], bar_period=num, time_period=num
                ) for num in range(1, 8)
            } for i in ins
        }
        # We delete the onset maker and item variables here as we no longer need to refer to them: this saves memory!
        del self.om

    def _extract_instrument_metadata(self, x_ins: str):
        def __get_onset_evaluation_data() -> dict:
            """Gets information from manual onset annotations, e.g. F-score"""
            cols = ['f_score', 'precision', 'recall', 'mean_asynchrony', 'fraction_matched']
            if not self.om.onset_evaluation:
                return {col: np.nan for col in cols}
            else:
                return {col: i[0][col] for col in cols for i in self.om.onset_evaluation if i[0]['instr'] == x_ins}

        def __get_subjective_rating_data() -> dict:
            """Gets information from subjective ratings of this instrument"""
            cols = [f'rating_{x_ins}_audio', f'rating_{x_ins}_detection', 'rating_mix', 'rating_comments']
            return {col: self.item[col] for col in cols}

        def __get_channel_overrides() -> str:
            """Gets information on channel overrides (if any) used for this particular instrument"""
            try:
                return self.item['channel_overrides'][x_ins]
            except KeyError:
                return ''

        mcols = ['track_name', 'album_name', 'recording_year', 'pianist', 'mbz_id', 'time_signature', 'first_downbeat']
        return {
            # Get metadata from item dictionary using required columns
            **{col: self.item[col] for col in mcols},
            # Get generic track metadata
            'tempo': self.om.tempo,
            'instrument': x_ins,
            'performer': self.item['musicians'][utils.INSTRUMENTS_TO_PERFORMER_ROLES[x_ins]],
            'channel_overrides': __get_channel_overrides(),
            # Raw beats
            'interpolated_beats': self.num_interpolated[x_ins],
            'actual_beats': self.df[x_ins].notna().sum(),
            'missing_beats': self.df[x_ins].isna().sum(),
            # Cleaning metadata, e.g. missing beats
            'fraction_silent': self.om.silent_perc[x_ins],
            'missing_beats_fraction': self.df[x_ins].isna().sum() / self.df.shape[0],
            # Subjective rating data (will be NaN if no subjective ratings conducted for this item)
            **__get_subjective_rating_data(),
            # Manual annotation evaluation data (will be NaN if no manual annotations present)
            **__get_onset_evaluation_data()
        }


class BaseExtractor:
    """Base feature extraction class, with some methods that are useful for all classes"""

    def __init__(self):
        # These are the default functions we'll call on any array to populate our summary statistics dictionary
        # We have to define these inside __init__ otherwise they'll be overwritten in the child classes
        self.summary_funcs = dict(
            mean=np.nanmean,
            median=np.nanmedian,
            std=np.nanstd,
            var=np.nanvar,
            quantile25=self.quantile25,
            quantile75=self.quantile75,
            count=len,
            count_nonzero=self.count_nonzero,
        )
        self.summary_dict = {}

    @staticmethod
    def count_nonzero(x) -> int:
        """Simple wrapper around `np.count_nonzero` that removes NaN values from an array"""
        return np.count_nonzero(~np.isnan(x))

    @staticmethod
    def quantile25(x) -> float:
        """Simple wrapper around `np.nanquantile` with arguments set"""
        return np.nanquantile(x, 0.25)

    @staticmethod
    def quantile75(x) -> float:
        """Simple wrapper around `np.nanquantile` with arguments set"""
        return np.nanquantile(x, 0.75)

    def __bool__(self):
        """Overrides built-in boolean method to return whether the summary dictionary has been populated"""
        return len(self.summary_dict.keys()) < 0

    def __contains__(self, item: str):
        """Overrides built-in method to return item from summary dictionary by key"""
        return item in self.summary_dict.keys()

    def __iter__(self):
        """Overrides built-in method to return iterable of key-value pairs from summary dictionary"""
        return self.summary_dict.items()

    def __len__(self):
        """Overrides built-in method to return length of summary dictionary"""
        return len(self.summary_dict.keys())

    def __repr__(self) -> dict:
        """Overrides default string representation to print a dictionary of summary stats"""
        return json.dumps(self.summary_dict)

    def update_summary_dict(self, array_names, arrays, *args, **kwargs) -> None:
        """Update our summary dictionary with values from this feature. Can be overridden!"""
        for name, df in zip(array_names, arrays):
            self.summary_dict.update({f'{name}_{func_k}': func_v(df) for func_k, func_v in self.summary_funcs.items()})

    @staticmethod
    def get_between(arr, i1, i2) -> np.array:
        """From an array `arr`, get all onsets between an upper and lower bound `i1` and `i2` respectively"""
        return arr[np.where(np.logical_and(arr >= i1, arr <= i2))]

    @staticmethod
    def truncate_df(
            arr: pd.DataFrame | pd.Series,
            low: float,
            high: float,
            col: str = None,
            fill_nans: bool = False
    ) -> pd.DataFrame:
        """Truncate a dataframe or series between a low and high threshold.

        Args:
            arr (pd.DataFrame | pd.Series): dataframe to truncate
            low (float): lower boundary for truncating
            high (float): upper boundary for truncating. Must be greater than `low`.
            col (str): array to use when truncating. Must be provided if `isinstance(arr, pd.DataFrame)`
            fill_nans (bool, optional): whether to replace values outside `low` and `high` with `np.nan`

        Raises:
            AssertionError: if `high` < `low`

        Returns:
            pd.DataFrame

        """
        # If both our lower and higher thresholds are NaN, return the array without masking
        if all([np.isnan(low), np.isnan(high)]):
            return arr
        # If we only have one value in our dataframe, or every value is NaN, low = high: this doesn't affect things
        assert low <= high
        # If we've passed in a series, we have to deal with it in a slightly different way
        if isinstance(arr, pd.Series):
            if fill_nans:
                return arr.mask(~arr.between(low, high))
            else:
                return arr[lambda x: (low <= x) & (x <= high)]
        # Otherwise, if we've passed in a dataframe, we have to deal with it in a different way
        elif isinstance(arr, pd.DataFrame):
            # We must provide a column to use for truncating in this case
            if col is None:
                raise AttributeError('Must provide argument `col` with `isinstance(arr, pd.DataFrame)`')
            mask = (low <= arr[col]) & (arr[col] <= high)
            if fill_nans:
                return arr.mask(~mask)
            else:
                return arr[mask]


class IOISummaryStats(BaseExtractor):
    """Extracts various baseline summary statistics from an array of IOIs

    Args:
        my_onsets (pd.Series): onsets to compute summary statistics for
        use_bpms (bool, optional): convert IOIs into beat-per-minute values, i.e. 60 / IOI (defaults to False)
        iqr_filter (bool, optional): apply IQR range filtering to IOI/BPM values (defaults to False)

    """
    def __init__(self, my_onsets, **kwargs):
        super().__init__()
        if isinstance(my_onsets, np.ndarray):
            my_onsets = pd.Series(my_onsets)
        iois = my_onsets.diff()
        name = 'iois'
        # Divide 60 / IOI if we want to use BPM values instead
        if kwargs.get('use_bpms', False):
            iois = 60 / iois
            name = 'bpms'
        # Filter our IOIs using an IQR filter, if required
        if kwargs.get('iqr_filter', False):
            iois = utils.iqr_filter(iois, fill_nans=True)
            name += '_filter'
        # Add in some extra functions to our summary functions dictionary
        self.summary_funcs['binary_entropy'] = self.binary_entropy
        self.summary_funcs['npvi'] = self.npvi
        self.summary_funcs['lempel_ziv_complexity'] = self.lempel_ziv_complexity
        # Update the summary dictionary by obtaining results for every function in `self.summary_funcs`
        self.update_summary_dict([name], [iois])

    @staticmethod
    def binary_entropy(iois: pd.Series) -> float:
        """Extract the Shannon entropy from an iterable"""
        # We convert our IOIs into milliseconds here to prevent floating point numbers
        ms_arr = (iois * 1000).dropna().astype(int).to_numpy()
        # Get the counts and probabilities of our individual IOIs
        _, counts = np.unique(ms_arr, return_counts=True)
        probabilities = counts / len(ms_arr)
        # Calculate the entropy and return
        return -np.sum(probabilities * np.log2(probabilities))
        # Alternative method using SciPy, should yield identical results
        # return stats.entropy((ioi * 1000).dropna().astype(int).value_counts().squeeze(), base=2)

    @staticmethod
    def npvi(iois: pd.Series) -> float:
        """Extract the normalised pairwise variability index (nPVI) from an iterable"""
        # Drop NaN values and convert array to Numpy
        dat = iois.dropna().to_numpy()
        # If we only have one element in our array after dropping NaN values, we can't calculate nPVI, so return NaN
        if len(dat) <= 1:
            return np.nan
        # Otherwise, we can go ahead and return the nPVI value for the array
        return sum([abs((k - k1) / ((k + k1) / 2)) for (k, k1) in zip(dat, dat[1:])]) * 100 / (sum(1 for _ in dat) - 1)

    @staticmethod
    def lempel_ziv_complexity(iois: pd.Series) -> float:
        """Extract complexity from a binary sequence using Lempel-Ziv compression algorithm,"""
        def lz(binary: np.array) -> int:
            """Function code for Lempel-Ziv compression algorithm"""
            # Convert our sequence into binary: values below mean = 0, above mean = 1
            # Set starting values for complexity calculation
            u, v, w = 0, 1, 1
            v_max, complexity = 1, 1
            # Begin calculating LZ complexity
            while True:
                if binary[u + v - 1] == binary[w + v - 1]:
                    v += 1
                    if w + v >= len(binary):
                        complexity += 1
                        break
                else:
                    if v > v_max:
                        v_max = v
                    u += 1
                    if u == w:
                        complexity += 1
                        w += v_max
                        if w > len(binary):
                            break
                        else:
                            u = 0
                            v = 1
                            v_max = 1
                    else:
                        v = 1
            return complexity

        # Try and convert our sequence to binary
        try:
            binary_sequence = np.vectorize(lambda x: int(x > np.nanmean(iois)))(iois[~np.isnan(iois)])
        # If we only have NaNs in our array we'll raise an error, so catch this and return NaN
        except ValueError:
            return np.nan
        # We need a sequence with at least 3 items in to calculate LZ complexity, so catch this and return NaN
        else:
            if len(binary_sequence) < 3:
                return np.nan
            # If we've passed all these checks, we should be able to calculate LZ complexity; do so now and return
            else:
                return lz(binary_sequence)


class RollingIOISummaryStats(IOISummaryStats):
    """Extracts the statistics in `IOISummaryStatsExtractor` on a rolling basis, window defaults to 4 bars length"""

    def __init__(self, my_onsets: pd.Series, downbeats, bar_period: int = 4, **kwargs):
        super().__init__(my_onsets=my_onsets, **kwargs)
        if isinstance(my_onsets, np.ndarray):
            my_onsets = pd.Series(my_onsets)
        self.summary_dict.clear()
        self.bar_period = bar_period
        # We get our raw rolling statistics here
        self.rolling_statistics = self.extract_rolling_statistics(my_onsets, downbeats, **kwargs)
        # We redefine summary_funcs here, as we want to remove extra functions added in `IOISummaryStatsExtractor`
        self.summary_funcs = BaseExtractor().summary_funcs
        # Update the summary dictionary
        self.summary_dict['bar_period'] = bar_period
        self.update_summary_dict(self.rolling_statistics.keys(), self.rolling_statistics.values())

    def extract_rolling_statistics(self, my_onsets: pd.Series, downbeats: np.array, **kwargs) -> dict:
        """Extract rolling summary statistics across the given bar period"""
        results = {f'rolling_{func_k}': [] for func_k in self.summary_funcs.keys()}
        for bar_num, (i1, i2) in enumerate(zip(downbeats, downbeats[self.bar_period:]), 1):
            iois_between = pd.Series(self.get_between(my_onsets.values, i1, i2)).diff()
            # Divide 60 / IOI if we want to use BPM values instead
            if kwargs.get('use_bpms', False):
                iois_between = 60 / iois_between
            # Filter our IOIs using an IQR filter, if required
            if kwargs.get('iqr_filter', False):
                iois_between = utils.iqr_filter(iois_between, fill_nans=True)
            # Iterate through each of our summary functions
            for func_k, func_v in self.summary_funcs.items():
                # Try and apply the summary function to the IOIs, and return NaN on an error
                try:
                    results[f'rolling_{func_k}'].append(func_v(iois_between))
                # These are all the errors that can result from our summary functions with NaN arrays
                except (IndexError, ValueError, ZeroDivisionError):
                    results[f'rolling_{func_k}'].append(np.nan)
        return results


class EventDensity(BaseExtractor):
    """Extract various features related to event density, on both a per-bar and per-second basis.

    Args:
        my_onsets (pd.series): onsets to calculate event density for
        downbeats (np.array): array of times corresponding to the first beat of each bar
        time_period (str, optional): the timeframe to calculate event density over, defaults to '1s' (one second)
        bar_period (int, optional): the number of bars to calculate event density over, defaults to 1 (bar)

    """
    def __init__(
            self,
            my_onsets: pd.Series,
            downbeats: np.array,
            time_period: int = 1,
            bar_period: int = 1
    ):
        super().__init__()
        if isinstance(my_onsets, np.ndarray):
            my_onsets = pd.Series(my_onsets)
        # Set attributes
        self.time_period = f'{time_period}s'
        self.bar_period = bar_period
        # Extract event density
        self.per_second = self.extract_ed_per_second(my_onsets)
        self.per_bar = self.extract_ed_per_bar(my_onsets, downbeats)
        # Update our summary dictionary
        self.summary_dict['time_period'] = time_period
        self.summary_dict['bar_period'] = bar_period
        self.update_summary_dict(['ed_per_second', 'ed_per_bar'], [self.per_second['density'], self.per_bar['density']])

    def extract_ed_per_second(self, my_onsets) -> pd.DataFrame:
        """For every second in a performance, extract the number of notes played"""
        return (
            pd.DataFrame({'ts': pd.to_datetime(my_onsets, unit='s'), 'density': my_onsets})
            .set_index('ts')
            .resample(self.time_period, label='left')
            .count()
            .reset_index(drop=False)
        )

    def extract_ed_per_bar(self, my_onsets, quarter_note_downbeats) -> pd.DataFrame:
        """Extract the number of notes played within each specified bar period"""
        sequential_downbeats = zip(quarter_note_downbeats, quarter_note_downbeats[self.bar_period:])
        my_onsets_arr = my_onsets.to_numpy()
        matches = [
            {f'bars': f'{bar_num}-{bar_num + self.bar_period}', 'density': len(self.get_between(my_onsets_arr, i1, i2))}
            for bar_num, (i1, i2) in enumerate(sequential_downbeats, 1)
        ]
        return pd.DataFrame(matches)


class BeatUpbeatRatio(BaseExtractor):
    """Extract various features related to beat-upbeat ratios (BURs)"""
    def __init__(self, my_onsets, my_beats):
        super().__init__()
        if isinstance(my_onsets, np.ndarray):
            my_onsets = pd.Series(my_onsets)
        # Extract our burs here, so we can access them as instance properties
        self.bur = self.extract_burs(my_onsets, my_beats, use_log_burs=False)
        self.bur_log = self.extract_burs(my_onsets, my_beats, use_log_burs=True)
        # Update our summary dictionary
        self.update_summary_dict(['bur', 'bur_log'], [self.bur['burs'], self.bur_log['burs']])

    def extract_burs(
            self,
            my_onsets: np.array,
            my_beats: np.array,
            use_log_burs: bool = False
    ) -> pd.DataFrame:
        """Extracts beat-upbeat ratio (BUR) values from an array of onsets.

        The beat-upbeat ratio is introduced in [1] as a concept for analyzing the individual amount of 'swing' in two
        consecutive eighth note beat durations. It is calculated simply by dividing the duration of the first, 'long'
        eighth note beat by the second, 'short' beat. A BUR value of 2 indicates 'perfect' swing, i.e. a triplet quarter
        note followed by a triplet eighth note, while a BUR of 1 indicates 'even' eighth note durations.

        Arguments:
            my_onsets (np.array, optional): the array of raw onsets.
            my_beats (np.array, optional): the array of crotchet beat positions.
            use_log_burs (bool, optional): whether to use the log^2 of inter-onset intervals to calculate BURs,
                as employed in [2]. Defaults to False.

        Returns:
            np.array: the calculated BUR values

        References:
            [1]: Benadon, F. (2006). Slicing the Beat: Jazz Eighth-Notes as Expressive Microrhythm. Ethnomusicology,
                50/1 (pp. 73-98).
            [2]: Corcoran, C., & Frieler, K. (2021). Playing It Straight: Analyzing Jazz Soloists’ Swing Eighth-Note
                Distributions with the Weimar Jazz Database. Music Perception, 38(4), 372–385.

        """

        # Use log2 burs if specified
        func = lambda a: a
        if use_log_burs:
            from math import log2 as func

        def bur(a: float, b: float) -> float:
            """BUR calculation function"""
            # Get the onsets between our first and second beat (a and b)
            match = self.get_between(my_onsets, a, b)
            # If we have a group of three notes (i.e. three quavers), including a and b
            if len(match) == 3:
                # Then return the BUR (otherwise, we return None, which is converted to NaN)
                return func((match[1] - match[0]) / (match[2] - match[1]))
            else:
                return np.nan

        # If we're using Pandas, convert our onsets array to numpy for processing
        if isinstance(my_onsets, pd.Series):
            my_onsets = my_onsets.to_numpy()
        # Iterate through consecutive pairs of beats and get the BUR
        burs = [bur(i1, i2) for i1, i2 in zip(my_beats, my_beats[1:])]
        # We can't know the BUR for the final beat, so append NaN
        burs.append(np.nan)
        return pd.DataFrame({'beat': pd.to_datetime(my_beats, unit='s'), 'burs': burs})


class TempoSlope(BaseExtractor):
    """Extract features related to tempo slope, i.e. instantaneous tempo change (in beats-per-minute) per second"""
    def __init__(self, my_beats: pd.Series):
        super().__init__()
        my_bpms = 60 / my_beats.diff()
        self.model = self.extract_tempo_slope(my_beats, my_bpms)
        self.update_summary_dict([], [])

    @staticmethod
    def extract_tempo_slope(my_beats: np.array, my_bpms: np.array) -> RegressionResultsWrapper | None:
        """Create the tempo slope regression model"""
        # Dependent variable: the BPM measurements
        y = my_bpms
        # Predictor variable: the onset time (with an added intercept
        x = sm.add_constant(my_beats)
        # Fit the model and return
        try:
            return sm.OLS(y, x, missing='drop').fit()
        # These are all the different error types that can emerge when fitting to data with too many NaNs
        except (ValueError, IndexError, KeyError):
            return None

    def update_summary_dict(self, array_names, arrays, *args, **kwargs) -> None:
        """Update the summary dictionary with tempo slope and drift coefficients"""
        self.summary_dict.update({
            f'tempo_slope': self.model.params[1] if self.model is not None else np.nan,
            f'tempo_drift': self.model.bse[1] if self.model is not None else np.nan
        })


class Asynchrony(BaseExtractor):
    """Extracts various features relating to asynchrony of onsets.

    Many of these features rely on the definitions established in the `onsetsync` package (Eerola & Clayton, 2023),
    and are ported to Python here with minimal changes.

    """
    # TODO: implement some sort of way of calculating circular statistics here?

    def __init__(self, my_beats: pd.Series, their_beats: pd.DataFrame | pd.Series):
        super().__init__()
        # For many summary functions, we just need the asynchrony columns themselves
        self.summary_funcs.update(dict(
            pairwise_asynchronization=self.pairwise_asynchronization,
            groupwise_asynchronization=self.groupwise_asynchronization,
            mean_absolute_asynchrony=self.mean_absolute_asynchrony,
            mean_pairwise_asynchrony=self.mean_pairwise_asynchrony
        ))
        self.extract_asynchronies(my_beats, their_beats)
        # We calculate mean relative asynchrony slightly differently to other variables
        mra = self.mean_relative_asynchrony(my_beats, their_beats)
        self.summary_dict.update({'mean_relative_asynchrony': mra})

    @staticmethod
    def pairwise_asynchronization(asynchronies: pd.Series) -> float:
        """Extract the standard deviation of the asynchronies of a pair of instruments.

        Eerola & Clayton (2023) use the sample standard deviation rather than the population standard deviation, so we
        are required to set the correction term `ddof` in `np.nanstd` to 1 to correct this.

        Parameters:
            asynchronies (np.array): the onset time differences between two instruments

        Returns:
            float

        """
        return np.nanstd(asynchronies.dropna(), ddof=1)

    @staticmethod
    def groupwise_asynchronization(asynchronies: pd.Series) -> float:
        """Extract the root-mean-square (RMS) of the pairwise asynchronizations."""
        # Convert to a list for use in functools
        asynchronies_ = asynchronies.dropna().to_list()
        # We define the function (d^i/n)^2 here, for asynchrony d at the ith time point, with n total asynchrony values
        func = lambda total, asy: total + ((asy / len(asynchronies_)) ** 2)
        # Calculate all function values, then take the square root
        return np.sqrt(reduce(func, [0] + asynchronies_))

    @staticmethod
    def mean_absolute_asynchrony(asynchronies: pd.Series) -> float:
        """Extract the mean of all unsigned asynchrony values."""
        return asynchronies.dropna().abs().mean()
        # Alternative, should lead to identical results
        # return (1 / len(asynchronies)) * sum([abs(a) for a in asynchronies])

    @staticmethod
    def mean_pairwise_asynchrony(asynchronies: pd.Series) -> float:
        """Extract the mean of all signed asynchrony values."""
        return asynchronies.dropna().mean()
        # Alternative, should lead to identical results
        # return (1 / len(asynchronies)) * sum(asynchronies)

    def mean_relative_asynchrony(self, my_beats, their_beats: pd.Series | pd.DataFrame) -> float:
        """Extract the mean position of an instrument's onsets relative to the average position of the group"""
        # Get the average position of the whole group
        average_group_position = pd.concat([my_beats, their_beats], axis=1).dropna().mean(axis=1)
        # Get the relative position in comparison to the average: my_onset - average_onsets
        my_relative_asynchrony = my_beats - average_group_position
        # Return the mean asynchrony from our relative asynchrony
        return self.mean_pairwise_asynchrony(my_relative_asynchrony)

    def extract_asynchronies(self, my_beats: pd.Series, their_beats: pd.DataFrame | pd.Series) -> dict:
        """Extract asynchrony between an instrument of interest and all other instruments and calculate functions"""
        if isinstance(their_beats, pd.Series):
            their_beats = pd.DataFrame
        # Iterate through all instruments in the ensemble
        for partner_instrument in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys():
            # We can't have asynchrony to our own performance, so append NaN in these cases
            if partner_instrument == my_beats.name:
                for func_k, func_v in self.summary_funcs.items():
                    self.summary_dict[f'{partner_instrument}_async_{func_k}'] = np.nan
            # Otherwise, calculate the asynchrony to this instrument
            else:
                partner_beats = their_beats[partner_instrument]
                # Calculate asynchrony: my_onset - partner_onset, then drop NaN values
                asynchronies = my_beats - partner_beats
                # Update our summary dictionary
                for func_k, func_v in self.summary_funcs.items():
                    self.summary_dict[f'{partner_instrument}_async_{func_k}'] = func_v(asynchronies)


class PhaseCorrection(BaseExtractor):
    """Extract various features related to phase correction

    Args:
        my_beats (pd.Series): onsets of instrument to model
        their_beats (pd.DataFrame | pd.Series, optional): onsets of other instrument(s), defaults to None
        order (int, optional): the order of the model to create, defaults to 1 (i.e. 1st-order model, no lagged terms)
        iqr_filter (bool, optional): whether to apply an iqr filter to data, defaults to False
        difference_iois (bool, optional): whether to take the first difference of IOI values, defaults to True

    """

    def __init__(
            self,
            my_beats: pd.Series,
            their_beats: pd.DataFrame | pd.Series = None,
            order: int = 1,
            **kwargs,
    ):
        super().__init__()
        self.order = order
        self.iqr_filter = kwargs.get('iqr_filter', False)
        self.difference_iois = kwargs.get('difference_iois', True)
        self.standardize = kwargs.get('standardize', False)
        # Threshold dataframe based on provided low and high threshold
        self.low_threshold, self.high_threshold = kwargs.get('low_threshold', None), kwargs.get('high_threshold', None)
        # Create an empty variable to hold the data actually going into the model
        self.model_data = None
        # Create the model
        self.model = self.generate_model(my_beats, their_beats)
        # Create the dataframe and model summary information
        self.df = pd.DataFrame(self.extract_model_coefficients())
        self.summary_dict = self.df.to_dict(orient='records')

    def truncate(self, my_beats, their_beats) -> tuple:
        """Truncates our input data between given low and high thresholds"""
        # If we haven't set a lower and upper threshold, don't threshold the data
        if self.low_threshold is None and self.high_threshold is None:
            return my_beats, their_beats
        threshold = self.truncate_df(
            pd.concat([my_beats, their_beats], axis=1),
            col=my_beats.name,
            # If we haven't provided a low or high threshold, we want to use all the data
            low=self.low_threshold if self.low_threshold is not None else my_beats.min(numeric_only=True),
            high=self.high_threshold if self.high_threshold is not None else my_beats.max(numeric_only=True)
        )
        # Apply the threshold to `my_beats`
        my_beats = threshold[my_beats.name]
        # If we haven't passed in any data as `their_beats`, then break here
        if their_beats is None:
            return my_beats, their_beats
        # Otherwise, go ahead and threshold every column in `their_beats`
        their_beats = threshold[their_beats.columns if isinstance(their_beats, pd.DataFrame) else their_beats.name]
        return my_beats, their_beats

    def format_async_arrays(self, their_beats: pd.Series | pd.DataFrame | None, my_beats: pd.Series) -> pd.DataFrame:
        """Format our asynchrony columns"""
        # If we haven't specified any asynchrony terms, i.e. we want a restricted model
        if their_beats is None:
            return pd.DataFrame([], [])
        # If we've only passed in one asynchrony term as a series, convert it to a dataframe
        elif isinstance(their_beats, pd.Series):
            their_beats = pd.DataFrame(their_beats)
        results = []
        for partner_instrument in their_beats.columns:
            partner_onsets = their_beats[partner_instrument]
            # In the phase correction model, the asynchrony terms are our partner's asynchrony with relation to us,
            # i.e. their_onset - my_onset (from their perspective, this is my_onset - their_onset).
            # Normally we would calculate asynchrony instead as my_onset - their_onset.
            asynchronies = partner_onsets - my_beats
            # Format our asynchrony array by adding IQR filter, etc.; we don't want to difference them, though
            asynchronies_fmt = self.format_array(asynchronies, difference_iois=False)
            asynchronies_fmt.name = f'{my_beats.name}_{partner_instrument}_asynchrony'
            # Shift our formatted asynchronies variable by the correct amount and extend the list
            results.extend(list(self.shifter(asynchronies_fmt)))
        return pd.concat(results, axis=1)

    def format_array(
            self,
            arr: np.array,
            iqr_filter: bool = None,
            difference_iois: bool = None,
            standardize: bool = None
    ) -> pd.Series:
        """Applies formatting to a single array used in creating the model"""
        # Use the default settings, if we haven't overridden them
        if difference_iois is None:
            difference_iois = self.difference_iois
        if iqr_filter is None:
            iqr_filter = self.iqr_filter
        if standardize is None:
            standardize = self.standardize
        if arr is None:
            return
        # Save the name of the array here
        name = arr.name
        # Apply differencing to the column (only for inter-onset intervals)
        if difference_iois:
            arr = arr.diff()
        # Apply the IQR filter, preserving the position of NaN values
        if iqr_filter:
            arr = pd.Series(utils.iqr_filter(arr, fill_nans=True))
        # Convert the score to standardized values (Z-score)
        if standardize:
            arr = stats.zscore(arr)
        # Restore the name of the array and return
        arr.name = name
        return arr

    def shifter(self, arr: np.array) -> Generator:
        """Shift an input array by the required number of beats and return a generator"""
        for i in range(self.order):
            pi = arr.shift(i)
            # Update the name of the array
            pi.name = f"{str(arr.name)}_lag{i}"
            yield pi

    def generate_model(
            self,
            my_beats: pd.Series,
            their_beats: pd.DataFrame | pd.Series | None
    ) -> RegressionResultsWrapper:
        """Generate the phase correction linear regression model"""
        # Truncate incoming data based on set thresholds
        my_beats, their_beats = self.truncate(my_beats, their_beats)
        # Get my previous inter-onset intervals from my onsets and format
        my_prev_iois = my_beats.diff()
        my_prev_iois = self.format_array(my_prev_iois)
        my_prev_iois.name = f'{my_beats.name}_prev_ioi'
        # Get my next inter-onset intervals by shifting my previous intervals (dependent variable)
        y = my_prev_iois.shift(-1)
        y.name = f'{my_beats.name}_next_ioi'
        # Get array of 'previous' inter-onset intervals (independent variable #1)
        my_prev_iois = pd.concat(list(self.shifter(my_prev_iois)), axis=1)
        # Get arrays of asynchrony values (independent variables #2, #3)
        async_arrs = self.format_async_arrays(their_beats, my_beats)
        # Combine independent variables into one dataframe and add constant term
        x = pd.concat([my_prev_iois, async_arrs], axis=1)
        x = sm.add_constant(x)
        # Update our instance attribute here, so we can debug the data going into the model, if needed
        self.model_data = pd.concat([my_beats, their_beats, x, y], axis=1)
        # Fit the regression model and return
        try:
            return sm.OLS(y, x, missing='drop').fit()
        # These are all the different error types that can emerge when fitting to data with too many NaNs
        except (ValueError, KeyError, IndexError):
            return None

    def extract_model_coefficients(self) -> Generator:
        """Extracts coefficients from linear phase correction model and format them correctly"""
        def extract_endog_instrument() -> str:
            """Returns name of instrument used in dependent variable of the model"""
            ei = self.model.model.endog_names.split('_')[0].lower()
            # Check that the value we've returned is contained within our list of possible instruments
            assert ei in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()
            return ei

        def getter(st: str) -> float:
            """Tries to get a coupling value from a given string"""
            try:
                return model_params[st]
            except KeyError:
                return np.nan

        # These are all basic statsmodels attributes we can extract easily from the model
        attributes = ['nobs', 'rsquared', 'rsquared_adj', 'aic', 'bic', 'llf']
        # If the model did not compile, return a dictionary filled with NaNs for every variable
        if self.model is None:
            extra_vars = ['intercept', 'resid_std', 'resid_len', 'self_coupling']
            instrs = utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()
            for lagterm in range(self.order):
                yield {
                    'phase_correction_order': self.order,
                    'phase_correction_lag': lagterm,
                    **{ke: np.nan for ke in attributes + extra_vars + [f'coupling_{i}' for i in instrs]}
                }
        # Otherwise, get basic model fit attributes from the model class
        else:
            # Extract the name of the endog instrument from our model
            endog_ins = extract_endog_instrument()
            # Convert our model parameters to a dictionary
            model_params = self.model.params.to_dict()
            # Iterate through every lag term
            for lagterm in range(self.order):
                yield {
                    'phase_correction_order': self.order,
                    'phase_correction_lag': lagterm,
                    'coupling_piano': getter(f'{endog_ins}_piano_asynchrony_lag{lagterm}'),
                    'coupling_bass': getter(f'{endog_ins}_bass_asynchrony_lag{lagterm}'),
                    'coupling_drums': getter(f'{endog_ins}_drums_asynchrony_lag{lagterm}'),
                    'self_coupling': model_params[f'{endog_ins}_prev_ioi_lag{lagterm}'],
                    'intercept': model_params['const'],
                    f'resid_std': np.std(self.model.resid),
                    f'resid_len': len(self.model.resid),
                    **{attribute: getattr(self.model, attribute) for attribute in attributes}
                }


class GrangerCausality(BaseExtractor):
    """Extracts various features related to Granger causality.

    Args:
        my_beats (pd.Series): onsets of instrument to model
        their_beats (pd.DataFrame | pd.Series): onsets of remaining instrument(s)
        order (int, optional): the order of the model to create, defaults to 1 (i.e. 1st-order model, no lagged terms)
        **kwargs: keyword arguments passed to `PhaseCorrectionExtractor`

    """

    def __init__(
            self,
            my_beats: pd.Series,
            their_beats: pd.DataFrame | pd.Series,
            order: int = 1,
            **kwargs
    ):
        super().__init__()
        self.order = order
        # Update the summary dictionary
        self.summary_dict = self.compute_granger_indexes(my_beats, their_beats, **kwargs)

    def compute_fisher_test(self, var_restricted: float, var_unrestricted: float, n: int) -> float:
        """Evaluate statistical significance of Granger test with Fisher test"""
        # Calculate degrees of freedom for the F-test
        df1 = self.order
        df2 = n - 2 * self.order
        # Calculate the F-statistic and associated p-value for the F-test
        f_statistic = ((var_restricted - var_unrestricted) / df1) / (var_unrestricted / df2)
        return float(1 - stats.f.cdf(f_statistic, df1, df2))

    def compute_granger_index(self, my_beats, their_beats, **kwargs) -> tuple[float, float]:
        """Compute the Granger index between a restricted (self) and unrestricted (joint) model"""
        # TODO: think about whether we want to compute GCI at every lag UP TO self.order and use smallest,
        #  or just at self.order (current)
        # Create the restricted (self) model: just the self-coupling term(s)
        restricted_model = PhaseCorrection(my_beats, order=self.order, **kwargs).model
        # Create the unrestricted (joint) model: the self-coupling and partner-coupling terms
        unrestricted_model = PhaseCorrection(my_beats, their_beats, order=self.order, **kwargs).model
        # In the case of either model breaking (i.e. if we have no values), return NaN for both GCI and p
        if restricted_model is None or unrestricted_model is None:
            return np.nan, np.nan
        # Otherwise, extract the variance from both models
        var_restricted = np.nanvar(restricted_model.resid)
        var_unrestricted = np.nanvar(unrestricted_model.resid)
        # Calculate the Granger-causality index: the log of the ratio between the variance of the model residuals
        gci = np.log(var_restricted / var_unrestricted)
        # Carry out the Fisher test and obtain a p-value
        p = self.compute_fisher_test(var_restricted, var_unrestricted, restricted_model.nobs)
        return gci, p

    def compute_granger_indexes(self, my_beats, their_beats: pd.DataFrame, **kwargs) -> dict:
        """Compute Granger indexes for given input array and all async arrays, i.e. for both possible leaders in trio"""
        di = {'granger_causality_order': self.order}
        for instrument in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys():
            # We can't compute Granger causality in relation to our own performance, so we yield an empty dictionary
            if instrument == my_beats.name:
                di.update({
                    f'granger_causality_{my_beats.name}_i': np.nan,
                    f'granger_causality_{my_beats.name}_p': np.nan,
                })
            else:
                gci, p = self.compute_granger_index(my_beats, their_beats[instrument], **kwargs)
                di.update({
                    f'granger_causality_{instrument}_i': gci,
                    f'granger_causality_{instrument}_p': p,
                })
        return di


class PartialCorrelation(BaseExtractor):
    """Extracts various features related to partial correlation between inter-onset intervals and onset asynchrony.

    This class calculates the partial correlation between (differenced) inter-onset intervals by musician `X` and
    lagged asynchronies between `X` and musician `Y`, controlling for prior (differenced) inter-onset intervals by `X`,
    i.e. accounting for the possibility of autocorrelated beat durations by `X`; see [1].

    Args:
        my_beats (pd.Series): onsets of instrument to model
        their_beats (pd.DataFrame | pd.Series): onsets of remaining instrument(s)
        order (int, optional): number of lag terms to calculate, defaults to 1
        iqr_filter (bool, optional): apply an iqr filter to inter-onset intervals, defaults to False
        difference_iois (bool, optional): whether to detrend inter-onset intervals via differencing, defaults to True

    References:
        [1]: Cheston, H. (2022). ‘Turning the beat around’: Time, temporality, and participation in the jazz solo break.
            Proceedings of the Conference on Interdisciplinary Musicology 2022: Participation, Edinburgh, UK.

    """
    iqr_filter = False
    difference_iois = True

    def __init__(self, my_beats: pd.Series, their_beats: pd.DataFrame | pd.Series, order: int = 1, **kwargs):
        super().__init__()
        self.order = order
        self.summary_dict = self.extract_partial_correlations(my_beats, their_beats, **kwargs)

    @staticmethod
    def partial_correlation(x: pd.Series, y: pd.Series, z: pd.Series):
        """Calculates partial correlation between arrays X and Y, controlling for the effect of Z

        Args:
            x (pd.Series): dependent variable
            y (pd.Series): independent variable
            z (pd.Series): control variable

        Returns:
            float

        """
        xy = x.corr(y, method='pearson')
        xz = x.corr(z, method='pearson')
        yz = y.corr(z, method='pearson')
        return (xy - (xz * yz)) / np.sqrt((1 - xz ** 2) * (1 - yz ** 2))

    @staticmethod
    def pvalue(n: int, k: int, r: float) -> float:
        """Extracts p-value from degrees of freedom and regression coefficient"""
        dof = n - k - 2
        tval = r * np.sqrt(dof / (1 - r ** 2))
        return 2 * stats.t.sf(np.abs(tval), dof)

    def extract_partial_correlations(
            self,
            my_beats: pd.Series,
            their_beats: pd.DataFrame | pd.Series,
            **kwargs
    ) -> dict:
        """Extracts partial correlation between inter-onset intervals and onset asynchrony at required lags"""
        # Get our initial inter-onset interval values
        my_differenced_iois = my_beats.diff()
        # Apply any filtering and further differencing as required
        if kwargs.get('difference_iois', self.difference_iois):
            my_differenced_iois = my_differenced_iois.diff()
        if kwargs.get('iqr_filter', self.iqr_filter):
            my_differenced_iois = pd.Series(utils.iqr_filter(my_differenced_iois, fill_nans=True))
        # Get our next inter-onset intervals: this is what we'll be predicting
        my_next_iois = my_differenced_iois.shift(-1)
        di = {'partial_corr_order': self.order}
        # Iterate through all instruments played by the other instruments in our group
        for instrument in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys():
            if instrument == my_beats.name:
                di.update({
                    f'partial_corr_{instrument}_r': np.nan,
                    f'partial_corr_{instrument}_p': np.nan,
                    f'partial_corr_{instrument}_n': np.nan,
                })
            else:
                # Get the asynchrony values between that instrument and ours
                my_asynchronies = their_beats[instrument] - my_beats
                # TODO: think about labelling of lag terms: is lag 0 really lag 0?
                # Shift our asynchronies and interval variables by the required lag term
                my_prev_asynchronies = my_asynchronies.shift(self.order)    # Independent variable
                my_prev_iois = my_differenced_iois.shift(self.order)    # Control variable
                # Construct the dataframe, drop NaN values, and set column titles
                df = pd.concat([my_next_iois, my_prev_asynchronies, my_prev_iois], axis=1).dropna()
                df.columns = ['my_next_iois', 'my_prev_asynchronies', 'my_prev_iois']
                # Create the partial correlation matrix and extract p-value
                # The results here should be identical to those given by the `pingouin.partial_corr` function
                pcorr = self.partial_correlation(x=df.my_next_iois, y=df.my_prev_asynchronies, z=df.my_prev_iois)
                pval = self.pvalue(df.shape[0], df.shape[1] - 2, pcorr)
                # Yield the results in a nice dictionary format
                di.update({
                    f'partial_corr_{instrument}_r': pcorr,
                    f'partial_corr_{instrument}_p': pval,
                    f'partial_corr_{instrument}_n': df.shape[0],
                })
        return di


class CrossCorrelation(BaseExtractor):
    """Extract features related to the cross-correlation of inter-onset intervals and onset asynchrony"""
    difference_iois = True
    iqr_filter = False

    def __init__(self, my_beats: pd.Series, their_beats: pd.DataFrame, order: int = 1, **kwargs):
        super().__init__()
        if not isinstance(their_beats, pd.DataFrame):
            their_beats = pd.DataFrame(their_beats)
        self.order = order
        self.summary_dict = self.extract_cross_correlations(my_beats, their_beats, **kwargs)

    def extract_cross_correlations(self, my_beats: pd.Series, their_beats: pd.DataFrame, **kwargs) -> dict:
        """Extract cross correlation coefficients at all lags up to `self.order`"""
        # Get inter-onset intervals from onsets and apply any additional filtering needed
        my_iois = my_beats.diff()
        if kwargs.get('difference_iois', self.difference_iois):
            my_iois = my_iois.diff()
        if kwargs.get('iqr_filter', self.iqr_filter):
            my_iois = pd.Series(utils.iqr_filter(my_iois, fill_nans=True))
        di = {'cross_corr_order': self.order}
        # Iterate through each instrument
        for instrument in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys():
            # We can't have cross-correlation with ourselves
            # TODO: investigate whether we want to provide auto-correlation here
            if instrument == my_beats.name:
                di.update({
                    f'cross_corr_{instrument}_r': np.nan,
                    f'cross_corr_{instrument}_p': np.nan,
                    f'cross_corr_{instrument}_n': np.nan,
                })
            else:
                # Get the asynchronies between us and this instrument
                asynchronies = their_beats[instrument] - my_beats
                # Lag the asynchronies, concatenate with IOIs, and drop NaN values
                combined = pd.concat([my_iois, asynchronies.shift(self.order)], axis=1).dropna()
                combined.columns = ['iois', 'asynchronies']
                # If, after dropping NaN values, we have fewer than 2 values, we can't calculate r, so return NaN
                if len(combined) < 2:
                    r, p = np.nan, np.nan
                # Otherwise, compute the correlation and return the necessary statistics
                else:
                    r, p = stats.pearsonr(combined['iois'], combined['asynchronies'])
                di.update({
                    f'cross_corr_{instrument}_r': r,
                    f'cross_corr_{instrument}_p': p,
                    f'cross_corr_{instrument}_n': int(combined.shape[0]),
                })
        return di


if __name__ == '__main__':
    pass
