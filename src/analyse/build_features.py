import json
import warnings

import librosa
import numpy as np
import soundfile as sf
import scipy.signal as signal
import pandas as pd
from mir_eval.onset import f_measure


class OnsetDetectionMaker:
    # These values are hard-coded and used throughout: we probably shouldn't change them
    sample_rate = 44100
    hop_length = 512
    # Define optimised defaults for onset_strength and onset_detect functions, for each instrument
    # These defaults were found through a parameter search against a reference set of onsets, annotated manually
    # TODO: we need to expand the number of recordings used in our parameter search
    # These are passed whenever onset_strength is called for this particular instrument's audio
    onset_strength_params = {
        'piano': dict(
            fmin=110,   # Minimum frequency to use
            fmax=4100,    # Maximum frequency to use
            center=False,    # Use left-aligned frame analysis in STFT
            max_size=1,    # Size of local maximum filter in frequency bins (1 = no filtering)
        ),
        'bass': dict(
            fmin=30,
            fmax=500,
            center=False,
            max_size=167,
        ),
        'drums': dict(
            fmin=3500,
            fmax=11000,
            center=False,
            max_size=1,
        )
    }
    # These are passed whenever onset_detect is called for this particular instrument's audio
    onset_detection_params = {
        'piano': dict(
            backtrack=False,    # Whether to roll back detected onset to nearest preceding minima, i.e. start of a note
            wait=3,    # How many samples must pass from one detected onset to the next
            delta=0.06,    # Hard threshold a sample must exceed to be picked as an onset
            pre_max=4,    # Number of samples to consider before a sample when computing max of a window
            post_max=4,    # Number of samples to consider after a sample when computing max of a window
            pre_avg=10,    # Number of samples to consider before a sample when computing average of a window
            post_avg=10    # Number of samples to consider after a sample when computing average of a window
        ),
        'bass': dict(
            backtrack=True,
            rms=True,
            wait=4,
            delta=0.04,
            pre_max=6,
            post_max=6,
            pre_avg=15,
            post_avg=15
        ),
        'drums': dict(
            backtrack=False,
            wait=4,
            delta=0.09,
            pre_max=6,
            post_max=6,
            pre_avg=19,
            post_avg=19
        )
    }
    data_dir = r'..\..\data'

    def __init__(
            self,
            item: dict,
            **kwargs
    ):
        self.item = item
        # Load our audio file in when we initialise the item: we won't be changing this much
        self.audio = self._load_audio(**kwargs)
        # Dictionary to hold arrays of onset envelopes for each instrument
        self.env = {}
        # Dictionary to hold arrays of detected onsets for each instrument
        self.ons = {}

    def _load_audio(
            self,
            **kwargs
    ) -> dict:
        """
        Wrapper around librosa.load_audio, called when class instance is constructed in order to generate audio for
        all instruments in required format. Keyword arguments are passed on to .load_audio
        """

        # These arguments are passed in whenever this class is constructed, i.e. to __init__
        duration = kwargs.get('duration', None)
        offset = kwargs.get('offset', 0)
        res_type = kwargs.get('res_type', 'soxr_vhq')
        mono = kwargs.get('mono', False)
        dtype = kwargs.get('dtype', np.float64)
        # Empty dictionary to hold audio
        audio = {}
        # Iterate through all the tracks and filepaths in the output key of our JSON item
        for track, fpath in self.item['output'].items():
            # Catch any UserWarnings that might be raised, usually to do with pysoundfile
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                y, sr = librosa.load(
                    path=fpath,
                    sr=self.sample_rate,
                    mono=mono,
                    offset=offset,
                    duration=duration,
                    dtype=dtype,
                    res_type=res_type,
                )
            audio[track] = y.T
        return audio

    def _beat_track_full_mix(self):
        pass

    def onset_strength(
            self,
            instr: str,
            aud: np.ndarray = None,
            **kwargs
    ) -> np.ndarray:
        """
        Wrapper around librosa.onset.onset_strength that allows for the use of per-instrument defaults. The required
        instrument (instr) must be passed as a string when calling this function. Any other keyword arguments should
        be accepted by librosa.onset.onset_strength, and can be passed to override per-instrument defaults.
        """

        # If we haven't passed any audio in, then construct this using the instrument name that we've passed
        if aud is None:
            aud = self.audio[instr].mean(axis=1)
        # Update our default parameters with any kwargs we've passed in
        self.onset_strength_params[instr].update(**kwargs)
        # Return the onset strength envelope using our default (i.e. hard-coded) sample rate and hop length
        return librosa.onset.onset_strength(
            y=aud,
            sr=self.sample_rate,
            hop_length=self.hop_length,
            **self.onset_strength_params[instr]
        )

    @staticmethod
    def _try_get_kwarg_and_remove(
            kwarg: str,
            kwargs: dict,
            default_=False
    ):
        """
        Simple wrapper function for kwargs.get() that will remove the given kwarg from kwargs after getting. Useful for
        other wrapper functions that take **kwargs as inputs that can be passed onto their parent function.
        """

        # Try and get the keyword argument from our dictionary of keyword arguments, with a default
        got = kwargs.get(kwarg, default_)
        # Attempt to delete the keyword argument from our dictionary of keyword arguments
        try:
            del kwargs[kwarg]
        except KeyError:
            pass
        # Return the keyword argument
        return got

    def onset_detect(
            self,
            instr: str,
            aud: np.ndarray = None,
            env: np.ndarray = None,
            units: str = 'time',
            **kwargs
    ) -> np.ndarray:
        """
        Wrapper around librosa.onset.onset_detect that enables per-instrument defaults to be used. Arguments passed as
        kwargs should be accepted by librosa.onset.onset_detect, with the exception of rms: set this to True to use a
        a custom energy function when backtracking detected onsets to local minima. Other keyword arguments overwrite
        current per-instrument defaults.
        """

        # If we haven't passed any input audio, get this now
        if aud is None:
            aud = self.audio[instr].mean(axis=1)
        # If we haven't passed an input onset envelope, get this now
        if env is None:
            env = self.env[instr]

        # Update the default parameters for the input instrument with any kwargs we've passed in
        self.onset_detection_params[instr].update(**kwargs)
        # The RMS argument can't be passed to .onset_detect(). We need to try and get it, then remove it from our dict
        rms = self._try_get_kwarg_and_remove(
            kwarg='rms',
            kwargs=self.onset_detection_params[instr],
            default_=False
        )
        # If we're backtracking onsets from the picked peak
        if self.onset_detection_params[instr]['backtrack']:
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
                **self.onset_detection_params[instr]
            )
        # If we're not backtracking, and using the picked peaks themselves
        else:
            return librosa.onset.onset_detect(
                y=aud,
                sr=self.sample_rate,
                hop_length=self.hop_length,
                units=units,
                onset_envelope=env,
                **self.onset_detection_params[instr]
            )

    def _bandpass_filter(
            self,
            y: np.ndarray,
            lowcut: int,
            highcut: int,
            order: int = 2
    ) -> np.ndarray:
        """
        Applies a bandpass filter with given low and high cut frequencies to an audio signal. Order is set to 2 by
        default as this seems to avoid some weird issues with the audio not rendering properly.
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
        return signal.lfilter(b, a, y)

    def output_click_track(
            self,
            instr: str,
            onsets: list = None,
            ext: str = 'wav',
            **kwargs
    ) -> None:
        """
        Outputs a track containing the filtered audio and detected onsets rendered as audio clicks. The onsets list
        should contain a list of arrays: these will be iterated through and converted to audio clicks with increasing
        output frequencies, enabling different onset detection algorithms to be compared. To only test the output
        audio used when detecting onsets, pass onsets as an empty list.
        """

        # Create a default list of onsets if we haven't passed one in ourselves
        if onsets is None:
            onsets = [self.ons[instr]]
        # Create an empty list to store our click track audio arrays
        clicks = []
        # Get the frequencies for each of our clicks
        start_freq = kwargs.get('start_freq', 750)    # The lowest click frequency
        width = kwargs.get('width', 100)    # The width of the click frequency: other frequencies attenuated
        # Iterate through all of our passed onsets, with a counter (used to increase click output frequency)
        for num, times in enumerate(onsets, 1):
            # Render the onsets to clicks, apply the bandpass filter, and append to our list
            clicks.append(
                self._bandpass_filter(
                    y=librosa.clicks(
                        times=times,
                        sr=self.sample_rate,
                        hop_length=self.hop_length,
                        length=len(self.audio[instr].mean(axis=1)),
                        click_freq=(start_freq * num)
                    ),
                    lowcut=(start_freq * num) - width,
                    highcut=(start_freq * num) + width
                )
            )
        # Filter the audio signal to only include the frequencies used in detecting onsets
        audio = self._bandpass_filter(
            y=self.audio[instr].mean(axis=1),
            lowcut=self.onset_strength_params[instr]['fmin'],
            highcut=self.onset_strength_params[instr]['fmax'],
        )
        # Sum the audio and click signals together
        process = audio + sum(clicks)
        # Create the audio file and save into the click tracks directory
        with open(rf'{self.data_dir}\processed\click_tracks\{self.item["fname"]}_{instr}_clicks.{ext}', 'wb') as f:
            sf.write(f, process, self.sample_rate)

    @staticmethod
    def compare_onset_detection_accuracy(
            fname: str,
            instr: str = None,
            onsets: list = None,
            onsets_name: list = None,
            window: float = 0.05
    ) -> dict:
        """
        Evaluates a given list of onset detection algorithm results, given as onsets, against an array of reference
        onsets. fname should be a filepath to a text file containing the detected onsets, as a single column (i.e. one
        onset per row, the default in Sonic Visualiser). window is the length of time wherein an onset is matched as
        'correct' against the reference.

        Returns a dataframe containing F-score, precision, and
        recall values, defined as:

            - Precision: number of true positive matches / (number of true positives + number of false positives)
            - Recall: number of true positive matches / (number of true positives + number of false negatives)
            - F-score:  (2 * precision * recall) / (precision + recall)

        Note that, for all metrics, greater values are suggestive of a stronger match between reference and estimated
        onsets, such that a score of 1.0 for any metric indicates equality.
        """

        # Generate the array of reference onsets from our passed file
        ref = np.genfromtxt(fname)[:, 0]
        # If we haven't provided any names for our different onset lists, create these now
        if onsets_name is None:
            onsets_name = [None for _ in range(len(onsets))]
        # Iterate through all the onset detection algorithms
        for (name, estimate) in zip(onsets_name, onsets):
            # Generate the F, precision, and recall values from mir_eval and yield as a dictionary
            f, p, r = f_measure(
                ref,
                estimate,
                window=window
            )
            yield {
                'name': name,
                'f_score': f,
                'precision': p,
                'recall': r,
                'instr': instr
            }


if __name__ == '__main__':
    with open(r'..\..\data\processed\processing_results.json', "r+") as in_file:
        corpus = json.load(in_file)
        # Iterate through each entry in the corpus, with the index as well
        for corpus_item in corpus:
            made = OnsetDetectionMaker(item=corpus_item)
            res = []
            for ins, freq in zip(['piano', 'bass', 'drums'], [3000, 1000, 1000]):
                made.env[ins] = made.onset_strength(ins)
                made.ons[ins] = made.onset_detect(ins)

                default = librosa.onset.onset_detect(
                    y=made.audio[ins].mean(axis=1),
                    units='time',
                    sr=made.sample_rate
                )
                made.output_click_track(
                    instr=ins,
                    onsets=[made.ons[ins], default],
                    start_freq=freq
                )
