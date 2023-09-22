#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Utility classes, functions, and variables used in the onset detection process."""

import os
import pickle
import warnings
from typing import Generator

import librosa
import numpy as np
import soundfile as sf
from madmom.features import DBNDownBeatTrackingProcessor, RNNDownBeatProcessor
from mir_eval.onset import f_measure
from mir_eval.util import match_events
from scipy import signal as signal

from src import utils


N_PLP_PASSES = 3    # This seems to lead to the best results after optimization
FREQUENCY_BANDS = {
    'piano': dict(
        fmin=110,  # Minimum frequency to use
        fmax=4100,  # Maximum frequency to use
    ),
    'bass': dict(
        fmin=30,
        fmax=500,
    ),
    'drums': dict(
        fmin=3500,
        fmax=11000,
    ),
    'mix': dict(
        fmin=20,
        fmax=20000,
    ),
}


class OnsetMaker:
    """Automatically detect onset and beat positions for each instrument in a single item in the corpus."""
    # These values are hard-coded and used throughout: we probably shouldn't change them
    sample_rate = utils.SAMPLE_RATE
    hop_length = utils.HOP_LENGTH
    # I think these are justifiable: Corcoran and Frieler (2021) claim that, in jazz, a 32nd note will be perceived
    # as a grace note (part of) the next beat, rather than as part of the preceding beat.
    detection_note_values = dict(
        left=1/32,
        right=1/16
    )
    silence_threshold = 1 / 3  # Warn when more of a track is silent than this threshold
    # The threshold to use when matching onsets
    window = 0.05
    top_db = dict(
        piano=40,
        bass=30,
        drums=60,
    )
    # Define file paths
    references_dir: str = rf"{utils.get_project_root()}\references"
    data_dir: str = rf"{utils.get_project_root()}\data"
    reports_dir: str = rf"{utils.get_project_root()}\reports"

    def __init__(
            self,
            corpus_name: str = 'corpus_bill_evans',
            item: dict = None,
            **kwargs
    ):
        # Set inputs as class parameters
        self.item = item
        self.corpus_name = corpus_name
        # Define optimised defaults for onset_strength and onset_detect functions, for each instrument
        # These defaults were found through a parameter search against a reference set of onsets, annotated manually
        self.onset_strength_params, self.onset_detect_params = self.return_converged_paramaters()
        # Construct the default file paths where our audio is saved
        self.instrs = {
            'mix': rf'{self.data_dir}\raw\audio\{self.item["fname"]}.{utils.AUDIO_FILE_FMT}',
            'piano': rf'{self.data_dir}\processed\spleeter_audio\{self.item["fname"]}_piano.{utils.AUDIO_FILE_FMT}',
            'bass': rf'{self.data_dir}\processed\demucs_audio\{self.item["fname"]}_bass.{utils.AUDIO_FILE_FMT}',
            'drums': rf'{self.data_dir}\processed\demucs_audio\{self.item["fname"]}_drums.{utils.AUDIO_FILE_FMT}'
        }
        # Dictionary to hold arrays of onset envelopes for each instrument
        self.env = {}
        # Dictionary to hold arrays of detected onsets for each instrument
        self.ons = {}
        # Empty attribute to hold our tempo
        self.tempo = None
        # Empty dictionary to hold the percentage of silence in each track
        self.silent_perc = {}
        # Empty attribute to hold our matched onset dictionary
        self.summary_dict = {}
        # Empty attribute to hold our evaluation with a reference
        self.onset_evaluation: list = []
        # Load our audio file in when we initialise the item: we won't be changing this much
        if self.item is not None:
            self.audio = self._load_audio(**kwargs)

    def __repr__(self):
        """Overrides default method so that summary dictionary is printed when the class is printed"""
        return repr(self.summary_dict)

    def return_converged_paramaters(self) -> tuple[dict, dict]:
        def fmt(val):
            if isinstance(val, bool):
                return val
            elif int(val) == val:
                return int(val)
            else:
                return float(val)

        onset_detect_args = [
            *utils.return_function_kwargs(librosa.util.peak_pick),
            *utils.return_function_kwargs(DBNDownBeatTrackingProcessor.__init__),
            *utils.return_function_kwargs(self.beat_track_rnn),
            *utils.return_function_kwargs(librosa.onset.onset_detect)
        ]
        onset_strength_args = utils.return_function_kwargs(librosa.onset.onset_strength)
        od_fmt, os_fmt = {}, {}
        js = utils.load_json(
            fpath=fr'{self.references_dir}\parameter_optimisation\{self.corpus_name}', fname='converged_parameters'
        )
        for item in js:
            od_fmt[item['instrument']] = {k: fmt(v) for k, v in item.items() if k in onset_detect_args}
            os_fmt[item['instrument']] = {k: fmt(v) for k, v in item.items() if k in onset_strength_args}
            os_fmt[item['instrument']].update(FREQUENCY_BANDS[item['instrument']])
        return os_fmt, od_fmt

    def _load_audio(
            self,
            **kwargs
    ) -> dict:
        """Loads audio as a time-series array for all instruments + the raw mix.

        Wrapper around `librosa.load_audio`, called when class instance is constructed in order to generate audio for
        all instruments in required format. Keyword arguments are passed on to .load_audio

        Arguments:
            **kwargs: passed to `librosa.load_audio`

        Return:
            dict: each key-value pair corresponds to the loaded audio for one instrument, as an array

        Raises:
            UserWarning: when a greater portion of a track than given in OnsetMaker.silence_threshold is silent

        """
        # These arguments are passed in whenever this class is constructed, i.e. to __init__
        duration = kwargs.get('duration', None)
        offset = kwargs.get('offset', 0)
        res_type = kwargs.get('res_type', 'soxr_vhq')
        mono = kwargs.get('mono', False)
        dtype = kwargs.get('dtype', np.float64)
        # Empty dictionary to hold audio
        audio = {}
        # Iterate through all the source separated tracks
        for name, fpath in self.instrs.items():
            # Catch any UserWarnings that might be raised, usually to do with different algorithms being used to load
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                y, sr = librosa.load(
                    path=self._get_channel_override_fpath(name, fpath),
                    sr=self.sample_rate,
                    mono=mono,
                    offset=offset,
                    duration=duration,
                    dtype=dtype,
                    res_type=res_type,
                )
            # Warn if our track exceeds silence threshold
            if name in self.top_db.keys():
                self.silent_perc[name] = self.get_silent_track_percent(y.T, top_db=self.top_db[name])
                if self.silent_perc[name] > self.silence_threshold:
                    warnings.warn(
                        f'item {self.item["fname"]}, track {name} exceeds silence threshold: '
                        f'({round(self.silent_perc[name], 2)} > {round(self.silence_threshold, 2)})'
                    )
            audio[name] = y.T
        return audio

    def _get_channel_override_fpath(
            self, name: str,
            fpath: str
    ) -> str:
        """Gets the filepath for an item, with any channel overrides specified.

        For instance, if we wish to use only the left channel for the double bass (and have specified "bass": "l" in the
        "channel_overrides" dictionary for this item in the corpus), this function will return the correct filepath
        pointing to the source-separated left channel file.

        Arguments:
            name (str): the name of the instrument
            fpath (str): the default filepath for the item (i.e. stereo audio)

        Returns:
            str: the overriden filepath if this is required and present locally, or the default (stereo) filepath if not

        """
        if 'channel_overrides' in self.item.keys():
            if name in self.item['channel_overrides'].keys():
                fp = fpath.replace(f'_{name}', f'-{self.item["channel_overrides"][name]}chan_{name}')
                if utils.check_item_present_locally(fp):
                    return fp
        return fpath

    def beat_track_rnn(
            self,
            passes: int = N_PLP_PASSES,
            starting_min: int = 100,
            # TODO: do we need to set this higher to account for extremely fast tracks, e.g. Peterson Tristeza?!
            starting_max: int = 300,
            use_nonoptimised_defaults: bool = False,
            audio_start: int = 0,
            audio_cutoff: int = None,
            **kwargs
    ) -> np.array:
        """Tracks the position of crotchet beats in the full mix of a track using recurrent neural networks.

        Wrapper around `RNNDownBeatProcessor' and 'DBNDownBeatTrackingProcessor` from `madmom.features.downbeat` that
        allows for per-instrument defaults and multiple passes. A 'pass' refers to taking the detected crotchets from
        one run of the network, cleaning the results, extracting features from the cleaned array (e.g. minimum and
        maximum tempi), then creating a new network using these features and repeating the estimation process. This
        narrows down the range of tempo values that can be detected and increases the accuracy of detected crotchets
        substantially over a period of several passes.

        Arguments:
            passes (int, optional): the number of estimation passes to use, defaults to 3.
            starting_min (int, optional): the minimum possible tempo (in BPM) to use for the first pass, defaults to 100
            starting_max (int, optional): the maximum possible tempo (in BPM) to use for the first pass, defaults to 300
            use_nonoptimised_defaults (bool, optional): use default parameters over optimised, defaults to False
            audio_start (int, optional): start reading audio from this point (in total seconds)
            audio_cutoff (int, optional): stop reading audio after this point (in total seconds)
            **kwargs: passed to `madmom.features.downbeat.DBNDownBeatTrackingProcessor`

        Returns:
            np.array: an array of detected crotchet beat positions from the final pass
        """

        # If we're using defaults, set kwargs to an empty dictionary
        kws = self.onset_detect_params['mix'] if not use_nonoptimised_defaults else dict()
        # Update our default parameters with any kwargs we've passed in
        kws.update(**kwargs)
        utils.try_get_kwarg_and_remove('passes', kws, default_=3)
        # Load in the audio file using soundfile, with the given audio cutoff point (defaults to no cutoff)
        if audio_cutoff is not None:
            audio_cutoff *= self.sample_rate
        samples, _ = sf.read(
            self.instrs['mix'],
            start=audio_start * self.sample_rate,
            stop=audio_cutoff,
            dtype='float64'
        )

        def tracker(
                tempo_min_: int = 100,
                tempo_max_: int = 300,
                **kws_
        ) -> tuple[np.array, np.array]:
            """Wrapper around classes from `madmom.features.downbeat`"""

            # Catch VisibleDeprecationWarnings that appear when creating the processor
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', np.VisibleDeprecationWarning)
                # Create the tracking processor
                proc = DBNDownBeatTrackingProcessor(
                    min_bpm=tempo_min_,
                    max_bpm=tempo_max_,
                    fps=100,
                    **kws_
                )
                # Fit the processor to the audio
                act = RNNDownBeatProcessor()(samples)
                # Return both the detected beat timestamps and the estimated position in a bar
                return proc(act)[:, 0], proc(act)[:, 1]

        # Create the first pass: this is designed to use a very low threshold and wide range of tempo values, enabling
        # the tempo to fluctuate a great deal; we will then use these results to narrow down the tempo in future passes
        timestamps, metre_positions = tracker(
            tempo_min_=starting_min,
            tempo_max_=starting_max,
            observation_lambda=2,
            beats_per_bar=[self.item['time_signature']],
            # We don't pass in our **kwargs here
            **dict(threshold=0, transition_lambda=75)
        )

        # Start creating our passes
        for i in range(1, passes):
            # Extract the BPM value for each IOI obtained from our most recent pass
            bpms = np.array([60 / p for p in np.diff(timestamps)])
            # Clean any outliers from our BPMs by removing values +/- 1.5 * IQR
            clean = utils.iqr_filter(bpms)
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)
                try:
                    # Extract mean, standard deviation, lower and upper quartiles
                    mean = np.nanmean(clean)
                    std = np.nanstd(clean)
                    low = np.nanpercentile(clean, 25)
                    high = np.nanpercentile(clean, 75)
                # If we didn't detect any onsets, the above lines will throw an error, so return an empty array
                except ValueError:
                    # TODO: we should probably log this somehow
                    return np.array([0])
            # If the distance between upper and lower bound is less than the distance between mean +/- std
            if high - low < (mean + std) - (mean - std):
                # Use upper and lower bounds as our maximum and minimum allowed tempo
                tempo_min, tempo_max = low, high
            else:
                # Use mean +/- 1 standard deviation as our maximum and minimum allowed tempo
                tempo_min, tempo_max = (mean - std), (mean + std)
            # Create the new pass, using the new maximum and minimum tempo
            timestamps, metre_positions = tracker(
                tempo_min_=tempo_min,
                tempo_max_=tempo_max,
                observation_lambda=16,
                beats_per_bar=[self.item['time_signature']],
                # Now we pass in our keyword arguments
                **kws
            )
        # Set the tempo value using the crotchet beat positions from our previous pass
        self.tempo = calculate_tempo(timestamps)
        return timestamps, metre_positions

    def onset_strength(
            self,
            instr: str,
            aud: np.array = None,
            use_nonoptimised_defaults: bool = False,
            **kwargs
    ) -> np.array:
        """Generates an onset strength envelope for a given instrument

        Wrapper around `librosa.onset.onset_strength` that allows for the use of per-instrument defaults. Any \*\*kwargs
        should be accepted by this function, and can be passed to override optimised per-instrument defaults.

        Arguments:
            instr (str): the name of the instrument to generate an onset strength envelope for
            aud (np.array, optional): an audio time-series array to generate the envelope for
            use_nonoptimised_defaults (bool, optional): whether to use default parameters, defaults to False
            **kwargs: any additional keyword arguments must be accepted by `librosa.onset.onset_strength`

        Returns:
            np.array: the onset strength envelope as an array

        """
        # If we haven't passed any audio in, then construct this using the instrument name that we've passed
        if aud is None:
            aud = self.audio[instr].mean(axis=1)
        # If we're using defaults, set kwargs to an empty dictionary
        kws = self.onset_strength_params[instr] if not use_nonoptimised_defaults else dict()
        # Update our default parameters with any kwargs we've passed in
        kws.update(**kwargs)
        # Suppress any user warnings that Librosa might throw
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            # Return the onset strength envelope using our default (i.e. hard-coded) sample rate and hop length
            return librosa.onset.onset_strength(
                y=aud,
                sr=self.sample_rate,
                hop_length=self.hop_length,
                **kws
            )

    def onset_detect(
            self,
            instr: str,
            aud: np.array = None,
            env: np.array = None,
            units: str = 'time',
            use_nonoptimised_defaults: bool = False,
            **kwargs
    ) -> np.array:
        """Detects onsets in an audio signal.

        Wrapper around `librosa.onset.onset_detect` that enables per-instrument defaults to be used. Arguments passed as
        kwargs should be accepted by `librosa.onset.onset_detect`, except for rms: set this to True to use a custom
        energy function when backtracking detected onsets to local minima. Other keyword arguments overwrite
        current per-instrument defaults.

        Arguments:
            instr (str): the name of the instrument to detect onsets in
            aud (np.array, optional): an audio time-series to detect onsets in
            env (np.array, optional): the envelope to use when detecting onsets
            units (str, optional): the units to return detected onsets in, defaults to 'time',
            use_nonoptimised_defaults (bool, optional): whether to use default parameters, defaults to False
            **kwargs: additional keyword arguments passed to `librosa.onset.onset_detect`

        Returns:
            np.array: the position of detected onsets

        """
        # If we haven't passed any input audio, get this now
        if aud is None:
            aud = self.audio[instr].mean(axis=1)
        # If we haven't passed an input onset envelope, get this now
        if env is None:
            env = self.env[instr]

        # Update the default parameters for the input instrument with any kwargs we've passed in
        self.onset_detect_params[instr].update(**kwargs)
        # The RMS argument can't be passed to .onset_detect(). We need to try and get it, then remove it from our dict
        rms = utils.try_get_kwarg_and_remove(
            kwarg='rms',
            kwargs=self.onset_detect_params[instr],
            default_=False
        )
        # If we're using defaults, set kwargs to an empty dictionary
        kws = self.onset_detect_params[instr] if not use_nonoptimised_defaults else dict()
        # Update our default parameters with any kwargs we've passed in
        kws.update(**kwargs)
        # If we're backtracking onsets from the picked peak
        if self.onset_detect_params[instr]['backtrack']:
            # If we want to use RMS values instead of our onset envelope when back tracking onsets
            if rms:
                energy = librosa.feature.rms(S=np.abs(librosa.stft(self.audio[instr].mean(axis=1))))[0]
            else:
                energy = env
            return librosa.onset.onset_detect(
                y=aud,
                sr=self.sample_rate,
                hop_length=self.hop_length,
                units=units,
                energy=energy,
                onset_envelope=env,
                **kws
            )
        # If we're not backtracking, and using the picked peaks themselves
        else:
            return librosa.onset.onset_detect(
                y=aud,
                sr=self.sample_rate,
                hop_length=self.hop_length,
                units=units,
                onset_envelope=env,
                **kws
            )

    def bandpass_filter(
            self,
            aud: np.array,
            lowcut: int,
            highcut: int,
            order: int = 2
    ) -> np.array:
        """Applies a bandpass filter with given low and high cut frequencies to an audio signal.

        Arguments:
            aud (np.array): the audio array to filter
            lowcut (int): the lower frequency to filter
            highcut (int): the higher frequency to filter
            order (int, optional): the filter order, defaults to 2 as this avoids weird issues with audio not rendering

        Returns:
            np.array: the filtered audio array

        """
        # Create the filter with the given values
        # Weird bug in PyCharm with signal.butter return here, so we disable checking for this statement
        # noinspection PyTupleAssignmentBalance
        b, a = signal.butter(
            order,
            [lowcut, highcut],
            fs=self.sample_rate,
            btype='band',
            output='ba'
        )
        # Apply the filter to the audio signal
        return signal.lfilter(b, a, aud)

    def generate_click_track(
            self,
            instr: str,
            onsets: list[np.array] = None,
            start_freq: int = 750,
            tag: str = 'clicks',
            folder: str = 'click_tracks',
            width: int = 100,
            **kwargs
    ) -> None:
        """Renders detected onsets to a click sound and outputs, combined with the original audio.

        Takes in a list of reference onset arrays, converts these to audible clicks, applies a bandpass filter (to make
        telling different onsets apart easier), filters the original audio to the frequencies considered when detecting
        onsets, then combines filtered original audio + click to a new audio track. This new click track can be helpful
        when comparing the results of different onset detection algorithms, or the overall accuracy of detected onsets.

        Arguments:
            instr (str): the name of the instrument to render audio from
            onsets (list[np.array]): a list of arrays, each containing detected onsets
            start_freq (int, optional): the starting frequency to render detected onsets to clicks, defaults to 750 (Hz)
            tag (str, optional): string placed at the end of the output filename, defaults to 'clicks'
            width (int, optional): the width of the bandpass filter applied to detected clicks, defaults to 100 (Hz)
            **kwargs: additional keyword arguments passed to `librosa.clicks`

        """
        # Create a default list of onsets if we haven't passed one in ourselves
        if onsets is None:
            onsets = [self.ons[instr]]
        # Create an empty list to store our click track audio arrays
        clicks = []
        # Iterate through all of our passed onsets, with a counter (used to increase click output frequency)
        for num, times in enumerate(onsets, 1):
            # Render the onsets to clicks, apply the bandpass filter, and append to our list
            clicks.append(
                self.bandpass_filter(
                    aud=librosa.clicks(
                        times=times[~np.isnan(times)],    # Remove any NaN values obtained from matching onsets & beats
                        sr=self.sample_rate,
                        hop_length=self.hop_length,
                        length=len(self.audio[instr].mean(axis=1)),
                        click_freq=(start_freq * num),
                        **kwargs
                    ),
                    lowcut=(start_freq * num) - width,
                    highcut=(start_freq * num) + width
                )
            )
        # Filter the audio signal to only include the frequencies used in detecting onsets
        audio = self.bandpass_filter(
            aud=self.audio[instr].mean(axis=1),
            lowcut=self.onset_strength_params[instr]['fmin'],
            highcut=self.onset_strength_params[instr]['fmax'],
        )
        # Sum the audio and click signals together
        process = audio + sum(clicks)
        # Create the audio file and save into the click tracks directory
        with open(rf'{self.reports_dir}\{folder}\{self.item["fname"]}_{instr}_{tag}.{utils.AUDIO_FILE_FMT}', 'wb') as f:
            sf.write(f, process, self. sample_rate)

    def compare_onset_detection_accuracy(
            self,
            ref: np.array = None,
            fname: str = None,
            instr: str = None,
            onsets: list[np.array] = None,
            onsets_name: list[str] = None,
            audio_cutoff: int = None,
            window: float = None,
            **kwargs
    ) -> dict:
        """Evaluates onset detection algorithm against reference onsets.

        For every onset detected by an algorithm, attempt to match these to the nearest onset in a reference set
        (usually obtained from manual annotation). Then, construct a summary dictionary, containing summary statistics
        relating to the precision, recall, and accuracy of the detection. For more information on the evaluation
        procedure, see `mir_eval.onset.f_measure`.

        At least one of ref or fname must be passed: ref must be an array of onset times, in seconds; fname must be a
        path to a text file containing onset times, with one onset per line. If both ref and fname are passed (don't do
        this), ref will take priority.

        Arguments:
            ref (np.array): an array of reference onsets (in seconds) to use for evaluation
            fname (str): the file path to a reference set of onsets, one onset per line
            instr (str): the name of an instrument or track
            onsets (list[np.array]): a list of arrays, each array should be the results from one algorithm
            onsets_name (list[str]): a list of names that should match with the algorithm results in onsets
            window (float): the size of the window used for matching each onset to a reference
            audio_cutoff (int, optional): stop reading audio after this point (in total seconds)
            **kwargs: additional key-value pairs passed to the returned summary dictionary

        Yields:
            dict: each dictionary contains summary statistics for one evaluation

        """
        def reader(fpath) -> Generator:
            """Simple file reader that gets the onset position from the output of Sonic Visualiser"""
            with open(fpath, 'r') as file:
                for line in file.readlines():
                    yield line.split('\t')[0]

        if ref is None and fname is None:
            raise AttributeError('At least one of ref, fname must be provided')
        # If we haven't passed in reference onsets but we have passed in a file path, generate the array from the file
        elif ref is None and fname is not None:
            ref = np.genfromtxt(reader(fname))
        # If we haven't provided any names for our different onset lists, create these now
        if onsets_name is None:
            onsets_name = [None for _ in range(len(onsets))]
        # If we haven't provided a window value, use our default (50 ms)
        if window is None:
            window = self.window
        # Iterate through all the onset detection algorithms passed in
        for (name, estimate) in zip(onsets_name, onsets):
            with warnings.catch_warnings():
                # If we've passed a cutoff value, remove all reference and estimated onsets above this
                if audio_cutoff is not None:
                    ref = np.array([i for i in ref if i < audio_cutoff])
                    estimate = np.array([i for i in estimate if i < audio_cutoff])
                # If we have no onsets, both mir_eval and numpy will throw warnings, so catch them here
                warnings.simplefilter('ignore', RuntimeWarning)
                warnings.simplefilter('ignore', UserWarning)
                # Calculate the mean asynchrony between the reference and estimate onsets
                matched = match_events(ref, estimate, window)
                # TODO: Is this throwing runtime warnings??
                mean_async = np.nanmean([estimate[e] - ref[r] for r, e in matched])
                # Generate the F, precision, and recall values from mir_eval and yield as a dictionary
                f, p, r = f_measure(ref, estimate, window=window)
            yield {
                'name': name,
                'instr': instr,
                'f_score': f,
                'precision': p,
                'recall': r,
                'mean_asynchrony': mean_async,
                'track': self.item['track_name'],
                'fraction_matched': len(matched) / len(ref),
                **kwargs
            }

    def match_onsets_and_beats(
            self,
            beats: np.array,
            onsets: np.array = None,
            instr: str = None,
            use_hard_threshold: bool = False,
            detection_note_values: dict = None
    ) -> np.array:
        """Matches event onsets with crotchet beat locations.

        For every beat in the iterable `beats`, find the closest proximate onset in the iterable `onsets`, within a
        given window. If no onset can be found within this window, set the matched onset to NaN. Window type can either
        be a hard, fixed value by setting `use_hard_threshold`, or flexible and dependant on a particular rhythmic value
        within the underlying tempo (set using the `detection_note_value` class attribute). The latter option is
        recommended and used as a default, given that hard thresholds for matching onsets at one tempo may not be
        appropriate for other tempi.

        Examples:
            >>> om = OnsetMaker()
            >>> bea = np.array([0, 0.5, 1.0, 1.5])
            >>> ons = np.array([0.1, 0.6, 1.25, 1.55])
            >>> print(om.match_onsets_and_beats(beats=bea, onsets=ons, use_hard_threshold=True, threshold=0.1))
            np.array([0.1 0.6 nan 1.55])

            >>> om = OnsetMaker()
            >>> om.tempo = 160
            >>> bea = np.array([0, 0.5, 1.0, 1.5])
            >>> ons = np.array([0.1, 0.6, 1.25, 1.55])
            >>> print(om.match_onsets_and_beats(beats=bea, onsets=ons, use_hard_threshold=False))
            np.array([nan nan nan 1.55])

        Arguments:
            beats (np.ndarray): iterable containing crotchet beat positions, typically tracked from the full mix
            onsets (np.ndarray): iterable containing onset positions, typically tracked from a source separated file
            instr (str): the name of an instrument, to be used if onsets is not provided
            use_hard_threshold (bool): whether to use a hard or tempo-dependent (default) threshold for matching onsets
            detection_note_values (dict): dictionary of note values to use either side of crotchet beat, e.g. 1/32, 1/8

        Returns:
            np.array: the matched onset array, with shape == len(beats)

        Raises:
            AttributeError: if neither onsets or instr are provided

        """
        def matcher() -> Generator:
            """Matching function. Matches the closest onset to each beat, within a window, returns a generator"""
            for beat in beats:
                # Subtract our onset array from our beat
                sub = onsets - beat
                re = []
                # Get the 'left onsets'; those played *before* the beat, and threshold them
                left = sub[sub < 0][np.abs(sub[sub < 0]) < l_threshold]
                # If we have left onsets, threshold them and get the one closest to the beat
                if len(left) > 0:
                    re.append(left.max())
                # Get the 'right onsets'; those played *after* the beat, and threshold them
                right = sub[sub >= 0][np.abs(sub[sub >= 0]) < r_threshold]
                # If we have right onsets, append the one closest to the beat to our list
                if len(right) > 0:
                    re.append(right.min())
                # Get the closest match from our left and right array
                try:
                    arr = np.array(re)
                    closest = arr[np.abs(arr).argmin()]
                    yield onsets[np.equal(sub, closest)][0]
                # If our array is empty (no left or right match), we'll get an error, so catch and return nan
                except ValueError:
                    yield np.nan

        # If we haven't passed an onsets list but we have passed an instrument as a string, try and get the onset list
        if onsets is None and instr is not None:
            onsets = self.ons[instr]
        # If we haven't passed an onset list or instrument string, raise an error
        if onsets is None and instr is None:
            raise AttributeError('At least one of onsets, instr must be provided')
        # Define the onset detection threshold: either hard or tempo-adjustable
        if use_hard_threshold:
            l_threshold = self.window
            r_threshold = self.window
        else:
            if detection_note_values is None:
                detection_note_values = self.detection_note_values
            l_threshold = ((60 / self.tempo) * 4) * detection_note_values['left']
            r_threshold = ((60 / self.tempo) * 4) * detection_note_values['right']
        # Return the list of matched onsets below our threshold
        return np.fromiter(matcher(), count=len(beats), dtype=np.float64)

    def generate_matched_onsets_dictionary(
            self,
            beats: np.array,
            onsets_list: list[np.array] = None,
            instrs_list: list = None,
            **kwargs
    ) -> dict:
        """Matches onsets from multiple instruments with crotchet beat positions and returns a dictionary.

        Wrapper function for `OnsetMaker.match_onsets_and_beats`. `onsets_list` should be a list of arrays corresponding
        to onset positions tracked from multiple source-separated instruments. These will then be sent individually to
        `OnsetMaker.match_onsets_and_beats` and matched with the provided `beats` array, then returned as the values in
        a dictionary, where the keys are identifiers passed in `instrs_list` (or numerical values, if this iterable is
        not passed). Any ``**kwargs`` will be passed to `OnsetMaker.match_onsets_and_beats`.

        Examples:
            >>> om = OnsetMaker()
            >>> bea = np.array([0, 0.5, 1.0, 1.5])
            >>> ons = [
            >>>     np.array([0.1, 0.6, 1.25, 1.55]),
            >>>     np.array([0.05, 0.45, 0.95, 1.45]),
            >>> ]
            >>> instrs = ['instr1', 'instr2']
            >>> print(om.generate_matched_onsets_dictionary(
            >>>     beats=bea, onsets_list=ons, instrs_list=instrs, use_hard_threshold=True, threshold=0.1)
            >>> )
            {
                'beats': array([0. , 0.5, 1. , 1.5]),
                'instr1': array([0.1 , 0.6 ,  nan, 1.55]),
                'instr2': array([0.05, 0.45, 0.95, 1.45])
            }

        Arguments:
            beats (np.array): iterable containing crotchet beat positions, typically tracked from the full mix
            onsets_list (list[np.array]): iterable containing arrays of onset positions
            instrs_list (list[str]): iterable containing names of instruments
            **kwargs: arbitrary keyword arguments, passed to `OnsetMaker.match_onsets_and_beats`

        Returns:
            dict: keys are instrument names, values are matched arrays

        Raises:
            AttributeError: if neither onsets_list or instrs_list are passed

        """
        # Get our required arguments
        if onsets_list and instrs_list is None:
            raise AttributeError('At least one of onsets_list and instrs_list must be provided')
        if onsets_list is None:
            onsets_list = [self.ons[ins_] for ins_ in instrs_list]
        if instrs_list is None:
            instrs_list = [i for i in range(len(onsets_list))]
        # Create the dictionary of crotchet beats and matched onsets, then return
        ma: dict = {'beats': beats}
        ma.update({
            name: self.match_onsets_and_beats(beats=beats, onsets=ons_, **kwargs) for ons_, name in
            zip(onsets_list, instrs_list)
        })
        return ma

    def get_nonsilent_sections(
            self,
            aud: np.array,
            thresh: float = 1,
            **kwargs
    ) -> np.array:
        """Returns the sections of a track which are not silent.

        Wrapper function for `librosa.effects.split` that returns slices of a given audio track that are not silent.
        Slices are only considered not silent if their duration is above a reference threshold, given in seconds: this
        is to prevent the parsing of many small slices of audio.

        Arguments:
            aud (np.array): array of audio, read in during construction of the OnsetMaker class
            thresh (float): value in seconds used when parsing slices
            **kwargs: arbitrary keyword arguments, passed to `librosa.effects.split`

        Returns:
            np.array: rows corresponding to sections of non-silent audio

        """
        # Get the sections of the track that are not silent
        non_silent = librosa.effects.split(
            librosa.util.normalize(aud).T,
            hop_length=self.hop_length,
            **kwargs
        )
        # Convert the non-silent sections (in frames) to time stamps
        to_ts = lambda s: s / self.sample_rate
        li = np.array([(to_ts(se[0]), to_ts(se[1])) for se in non_silent if to_ts(se[1]) - to_ts(se[0]) > thresh])
        # Combine slices of non-silent audio if the distance between the right and left edges is below the threshold
        try:
            roll = np.roll(li, 1)[1:, :]
        except IndexError:
            pass
        else:
            # Check the distance between the last element on one row and the first element on the next row
            for row in roll:
                # This will combine the slices if the distance is below threshold
                if row[1] - row[0] < thresh:
                    li[np.where(li == row[0])] = row[1]
        finally:
            return li

    def get_silent_track_percent(
            self,
            aud: np.array = None,
            silent: np.array = None,
            **kwargs
    ) -> float:
        """Returns the fraction of a track which is silent.

        Arguments:
            aud (np.array): array of audio, read in during construction of the OnsetMaker class
            silent (np.array): array of non-silent audio slices, returned from OnsetMaker.get_nonsilent_sections
            **kwargs: arbitrary keyword arguments, passed to `OnsetMaker.get_nonsilent_sections`

        Returns:
            float: the fraction of a track which is silent, e.g. 1 == a completely silent track

        Raises:
            AttributeError: if neither aud or silent are passed

        """
        # Catch any issues with arguments not being passed in
        if silent is None and aud is None:
            raise AttributeError('At least one of silent and aud must be passed!')
        # If we haven't generated our silent sections from our audio yet, do this now (and pass in any kwargs)
        if silent is None:
            silent = self.get_nonsilent_sections(aud=aud, **kwargs)

        # Get the overall duration of the track
        duration = librosa.get_duration(
            y=aud.T,
            sr=self.sample_rate,
            hop_length=self.hop_length
        )
        # Try to return the fraction of the track which is silent
        try:
            return 1 - (np.sum(silent[:, 1] - silent[:, 0]) / duration)
        # If sil is an empty list, that means the whole track is silent. So catch the resulting error and return 1
        except IndexError:
            return 1

    def remove_onsets_in_silent_passages(
            self,
            onsets: np.array,
            instr: str = None,
            silent: np.array = None,
            **kwargs
    ) -> np.array:
        """Removes onsets if they occurred during a portion of a track which was silent.

        For a given array of event onsets and a given array of non-silent audio slice timestamps, returns only those
        onsets which occurred during a portion of an audio track deemed not to be silent. This prevents any spurious
        onsets detected by Librosa from being included in an analysis.

        Examples:
            >>> om = OnsetMaker()
            >>> non_silent = np.array(
            >>>     [
            >>>         [0, 5],
            >>>         [10, 15]
            >>>     ]
            >>> )
            >>> ons_ = np.array([0.1, 0.6, 5.5, 12.5, 17.5])
            >>> print(om.remove_onsets_in_silent_passages(onsets=ons_, silent=non_silent))
            array([0.1, 0.6, 12.5])


        Arguments:
            onsets (np.array): an array of event onsets
            instr (str): the name of an instrument
            silent (np.array): an array of non-silent audio slices, returned from `OnsetMaker.get_nonsilent_sections`
            **kwargs: arbitrary keyword arguments, passed to `OnsetMaker.get_nonsilent_sections`

        Returns:
            np.array: an array of onset timestamps with those occurring during a silent slice removed

        Raises:
            AttributeError: if neither `silent` or `instr` are passed

        """
        # Catch errors if we haven't passed in the required arguments
        if silent is None and instr is None:
            raise AttributeError('At least one of silent or instr must be passed!')
        # If we haven't passed in our silent sections but have passed in our instrument name, get the silent sections
        if silent is None:
            silent = self.get_nonsilent_sections(
                self.audio[instr],
                top_db=self.top_db[instr],
                **kwargs
            )
        # Remove onsets from our onset list if they occurred during a silent section of the track and return the array
        clean = []
        for slice_ in silent:
            clean.extend([ons for ons in onsets if slice_[0] < ons < slice_[1]])
        return np.array(clean)

    def process_separated_audio(
            self,
            generate_click: bool,
            remove_silence: bool = True,
    ) -> None:
        """Process the separated audio for all of our individual instruments (piano, bass, drums)

        This is the central function for running processing on each source-separated audio file. It will generate an
        onset envelope, detect onsets within it, remove onsets from when the track was silent, compare the detections
        to a reference file (if this exists), generate a click track (if this is required), and match the detected
        onsets to the nearest crotchet beat. This function must be called AFTER `OnsetMaker.process_mixed_audio`, to
        ensure that the crotchet beat positions have been detected correctly in the raw audio mix.

        Parameters:
            generate_click (bool): whether to generate an audio click track
            remove_silence (bool): whether to remove onsets from portions of a track deemed to be silent by librosa

        """
        # Iterate through each instrument name
        for ins in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys():
            # Get the onset envelope for this instrument
            self.env[ins] = self.onset_strength(ins, use_nonoptimised_defaults=False)
            # Get the onsets
            self.ons[ins] = self.onset_detect(ins, env=self.env[ins], use_nonoptimised_defaults=False)
            # If we're removing onsets when the audio is silent, do that now
            if remove_silence:
                sil = self.get_nonsilent_sections(aud=self.audio[ins], top_db=self.top_db[ins])
                self.ons[ins] = self.remove_onsets_in_silent_passages(onsets=self.ons[ins], silent=sil)
            # If we have manually annotated onsets for this item, try and evaluate the accuracy of detected onsets
            try:
                eval_ = list(self.compare_onset_detection_accuracy(
                    fname=rf'{self.references_dir}\manual_annotation\{self.item["fname"]}_{ins}.txt',
                    onsets=[self.ons[ins]],
                    onsets_name=['optimised_librosa'],
                    instr=ins,
                ))
            except FileNotFoundError:
                pass
            else:
                self.onset_evaluation.append(eval_)
            # Match the detected onsets with our detected crotchet beats from the mad-mom output
            matched = self.match_onsets_and_beats(beats=self.ons['mix'], onsets=self.ons[ins])
            # Output our click track of detected beats + matched onsets
            if generate_click:
                self.generate_click_track(instr=ins, onsets=[self.ons[ins], matched], tag='beats', start_freq=750)

    def process_mixed_audio(
            self,
            generate_click: bool,
    ) -> None:
        """Process the raw audio mix, i.e. with all tracks together.

        This is the central function for running processing on the mixed audio. It will generate an onset envelope,
        detect crotchets within it using both predominant local pulse estimation and recurrent neural networks,
        compare the detections to a reference file (if this exists), and generate a click track (if this is
        required). This function should be called before `OnsetMaker.process_separated_audio`, to ensure that the
        crotchet beat positions are present before matching these to onsets detected in the source-separated tracks.

        Parameters:
            generate_click (bool): whether to generate an audio click track

        """
        # TODO: can we make the mix methods their own class, created in this function?
        # Track the beats using recurrent neural networks
        timestamps, metre_auto = self.beat_track_rnn(use_nonoptimised_defaults=False)
        self.ons['mix'] = timestamps
        # Estimate the metre automatically using the neural network results
        self.ons['metre_auto'] = metre_auto
        self.ons['downbeats_auto'] = self.extract_downbeats(timestamps, metre_auto)
        # Estimate the metre using a known downbeat and time signature
        self.ons['metre_manual'] = self.metre_from_annotated_downbeat(timestamps)
        self.ons['downbeats_manual'] = self.extract_downbeats(timestamps, self.ons['metre_manual'])
        # Warn if we're getting different results for automatic and manual metre detection
        if not all(self.ons['metre_auto'] == self.ons['metre_manual']):
            warnings.warn(f'item {self.item["fname"]}: manual and automatic metre detection diverge')
        # Try and get manual annotations for our crotchet beats, if we have them
        try:
            eval_ = list(self.compare_onset_detection_accuracy(
                fname=rf'{self.references_dir}\manual_annotation\{self.item["fname"]}_mix.txt',
                onsets=[self.ons['mix']],
                onsets_name=['madmom'],
                instr='mix',
            ))
        except FileNotFoundError:
            pass
        else:
            self.onset_evaluation.append(eval_)
        # Generate the click track for the tracked beats, including the manually-annotated downbeats
        if generate_click:
            self.generate_click_track(
                tag='beats', instr='mix', onsets=[self.ons['mix'], self.ons['downbeats_manual']], start_freq=1250
            )

    def finalize_output(
            self
    ) -> None:
        """Finalizes the output by cleaning up leftover files and setting any final attributes"""
        # Match the detected onsets together with the detected beats to generate our summary dictionary
        self.summary_dict = self.generate_matched_onsets_dictionary(
            beats=self.ons['mix'],
            onsets_list=[self.ons['piano'], self.ons['bass'], self.ons['drums']],
            instrs_list=['piano', 'bass', 'drums'],
            use_hard_threshold=False
        )
        self.summary_dict.update(dict(metre_auto=self.ons['metre_auto'], metre_manual=self.ons['metre_manual']))
        # Delete the raw audio as it will take up a lot of space when serialised
        del self.audio

    def metre_from_annotated_downbeat(
            self,
            timestamps_arr: np.array,
    ) -> np.array:
        """Constructs an array of metre positions from a known downbeat and time signature"""
        downbeat_position = np.argmin(np.abs(timestamps_arr - self.item['first_downbeat']))
        backwards_metre = []
        # First, we work backwards from the known downbeat
        val = self.item['time_signature']
        for i in reversed(range(downbeat_position)):
            if i >= 0:
                backwards_metre.append(float(val))
                val -= 1
                if val < 1:
                    val = self.item['time_signature']
        # Now, we work forwards from the annotated downbeat
        val = 1
        forwards_metre = []
        for i in range(len(timestamps_arr)):
            if i >= downbeat_position:
                forwards_metre.append(float(val))
                val += 1
                if val > self.item['time_signature']:
                    val = 1
        # Concatenate both arrays together
        return np.array(list(reversed(backwards_metre)) + forwards_metre)

    @staticmethod
    def extract_downbeats(
            beat_timestamps: np.array,
            beat_positions: np.array
    ) -> tuple[np.array, np.array]:
        """Takes in arrays of beat onsets and bar positions and returns the downbeats of each bar"""
        # Combine timestamps and bar positions into one array
        comb = np.array([beat_timestamps, beat_positions]).T
        # Create the boolean mask
        mask = (comb[:, 1] == 1)
        # Subset on the mask to get downbeats only and return
        return comb[mask, 0]


def get_tracks_with_manual_annotations(
        annotation_dir: str = fr'{utils.get_project_root()}\references\manual_annotation',
        annotation_ext: str = 'txt',
        corpus_json: list[dict] = None
) -> list:
    """Returns the IDs of tracks that should be annotated"""
    # return [t.strip('\n') for t in open(rf'{annotation_dir}\tracks_to_annotate.{annotation_ext}', 'r').readlines()]
    res = {}
    track_ids = '\t'.join([track['mbz_id'] for track in corpus_json])
    for file in os.listdir(annotation_dir):
        if file.endswith(annotation_ext):
            split = file.split('_')
            track_id = split[0].split('-')[-1]
            if track_id not in track_ids:
                continue
            try:
                res[split[0]].append(split[1].replace(f'.{annotation_ext}', ''))
            except KeyError:
                res[split[0]] = []
                res[split[0]].append(split[1].replace(f'.{annotation_ext}', ''))
    roles =  [*utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys(), 'mix']
    annotated_with_all_instrs = [k for k, v in res.items() if sorted(v) == sorted(roles)]
    return [t['mbz_id'] for t in corpus_json if t['fname'] in annotated_with_all_instrs]


def calculate_tempo(
        pass_: np.ndarray
) -> float:
    """Extract the average tempo from an array of times corresponding to crotchet beat positions"""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        return np.nanmean(np.array([60 / p for p in np.diff(pass_)]))


def get_cached_track_ids(fpath):
    """Open a pickle file and get the IDs of tracks that have already been processed"""
    data = []
    try:
        with open(f'{fpath}.p', 'rb') as fr:
            # Iteratively append from our Pickle file until we run out of data
            try:
                while True:
                    data.append(pickle.load(fr))
            except EOFError:
                pass
    except FileNotFoundError:
        return
    else:
        for track in data:
            yield track.item['mbz_id']


if __name__ == '__main__':
    pass