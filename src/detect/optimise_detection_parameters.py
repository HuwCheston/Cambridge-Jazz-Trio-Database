#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Optimises the parameters used in onset detection by running a large-scale search using ground truth files"""

import logging
from datetime import datetime
from itertools import product
from pathlib import Path

import click
import nlopt
import numpy as np
from dotenv import find_dotenv, load_dotenv
from joblib import Parallel, delayed

from src import utils
from src.detect.onset_utils import OnsetMaker


class Optimizer:
    """Base class for non-linear optimization of parameters"""
    joblib_backend = 'threading'
    # These attributes may be overridden in child classes
    audio_cutoff = None
    csv_name = ''
    n_jobs = -1

    def __init__(self, items: list[dict], instr: str, args: list[tuple], **kwargs):
        # Define the directory to store results from optimization in
        self.results_fpath = rf'{utils.get_project_root()}/references/parameter_optimisation'
        # The list of dictionaries containing item metadata for each track
        self.items = items
        # The name of the track we're optimizing
        self.instr = instr
        # Initialise the arguments for optimization
        # This should be in the form of a list of tuples, ("arg_name", type, lower_bound, upper_bound, initial_guess)
        self.args = args
        # Initialise the optimizer and pass in the length of our arguments
        self.opt = nlopt.opt(kwargs.get('algorithm', nlopt.LN_SBPLX), len(self.args))
        # Set the objective function: we're always trying to maximize the F-score
        self.opt.set_max_objective(self.objective_function)
        # Set upper and lower bounds for each argument
        self.opt.set_lower_bounds([i[2] for i in self.args])
        self.opt.set_upper_bounds([i[3] for i in self.args])
        # If we exceed this F-score, we treat the optimization as having converged (this will probably never happen)
        self.opt.set_stopval(kwargs.get('stopval', 0.999))
        # If the distance between two successive optimization results is less than this value, we've converged
        # 1e-4 seems to lead to best results from some initial testing, with the fewest number of redundant iterations
        self.opt.set_ftol_abs(kwargs.get('ftol_abs', 1e-4))
        self.opt.set_ftol_rel(kwargs.get('ftol_rel', 1e-4))
        # The maximum number of iterations, defaults to -1 (i.e. no maximum)
        self.opt.set_maxeval(kwargs.get('maxeval', -1))
        # The maximum time (in seconds) to optimize for, breaks after exceeding this, defaults to 60000 seconds (17 hrs)
        self.opt.set_maxtime(kwargs.get('maxtime', 60000))
        # Empty dictionary to store cached results from previous optimization runs in
        self.cached_results = []

    def return_kwargs(self, x: np.ndarray) -> dict:
        """Formats arguments from NLopt into the required keyword argument format"""
        return {fmt[0]: fmt[1](unfmt) for fmt, unfmt in zip(self.args, x)}

    def objective_function(self, x: np.ndarray, _) -> float:
        """Objective function for maximising F-score of detected onsets"""
        # Format our keyword arguments into the required format
        kwargs = self.return_kwargs(x)
        # Get the IDs and F-scores of tracks we've already processed with this set of parameters
        cached_ids, cached_fs = self.lookup_results_from_cache(params=kwargs)
        res = Parallel(n_jobs=self.n_jobs, backend=self.joblib_backend)(
            delayed(self.analyze_track)(item, **kwargs)
            for item in [item_ for item_ in self.items if item_['mbz_id'] not in cached_ids]
        )
        # Save the results from the previous iteration
        utils.save_csv(res, self.results_fpath, self.csv_name)
        # Extract F-scores from our cached and newly-generated results
        f_scores = cached_fs + [item['f_score'] for item in res]
        # Check that we've processed each of the items we need exactly once and that we've processed all the items
        assert sorted([item['mbz_id'] for item in res] + cached_ids) == sorted([item['mbz_id'] for item in self.items])
        assert len(f_scores) == len(self.items)
        # Log the results of this round of optimization
        self.log_iteration(cached_ids, f_scores)
        # Return value of the function to use in setting the next set of arguments in the optimizer function
        return np.nanmean(f_scores)

    def log_iteration(self, cached_ids: list, f_scores: list) -> None:
        """Log the results from a single iteration; overriden in child classes"""
        pass

    def analyze_track(self, item: dict, **kwargs) -> dict:
        """Placeholder for analysis function, run in parallel; overridden in child classes"""
        return

    def run_optimization(self) -> tuple[dict, float]:
        """Runs optimization in NLopt"""
        x_optimal = self.opt.optimize([i[4] for i in self.args])
        return self.return_kwargs(x_optimal), self.opt.last_optimum_value()

    def get_f_score(self, onsetmaker) -> float:
        """Returns F-score between detected onsets and manual annotation file"""
        fn = rf'{utils.get_project_root()}/references/manual_annotation/{onsetmaker.item["fname"]}_{self.instr}.txt'
        return onsetmaker.compare_onset_detection_accuracy(
            fname=fn, onsets=onsetmaker.ons[self.instr], audio_cutoff=self.audio_cutoff
        )['f_score']

    def lookup_results_from_cache(self, params: dict) -> tuple[list, list]:
        """Returns lists of IDs and F-scores for tracks that have already been processed with this set of parameters"""
        try:
            # Using a set here will drop any duplicates in case we've used the same kwargs across multiple iterations
            id_, f = zip(*set((c['mbz_id'], c['f_score']) for c in self.cached_results if params.items() <= c.items()))
            return list(id_), list(f)
        # If we don't have any tracks processed with this set of parameters, return two empty lists
        except ValueError:
            return [], []

    @staticmethod
    def enable_logger() -> logging.Logger:
        # We have to do this annoying logging thing to work with JobLib correctly...
        fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        logger = logging.getLogger(__name__)
        logging.basicConfig(level=logging.INFO, format=fmt)
        return logger


class OptimizeOnsetDetectCNN(Optimizer):
    """Optimizes the `OnsetMaker.onset_detect_cnn` function for a single instrument"""
    # These are the arguments we're passing into the optimizer
    fps = 100
    args = [
        ('threshold', float, 0, 10, 0.54),
        ('smooth', float, 0, 2, 0.05),
        ('pre_avg', float, 0, 2, 0),    # we may want to set this to 0 as the activation function comes from a nn
        ('post_avg', float, 0, 2, 0),    # see above
        ('pre_max', float, 0, 2, 1 / fps),
        ('post_max', float, 0, 2, 1 / fps)
    ]

    def __init__(self, items: dict, instr: str, **kwargs):
        super().__init__(items, instr, self.args, **kwargs)
        self.csv_name: str = f'onset_detect_cnn_{instr}'
        self.logger = self.enable_logger()
        try:
            self.cached_results = utils.load_csv(self.results_fpath, self.csv_name)
        except FileNotFoundError:
            pass

    def log_iteration(self, cached_ids: list, f_scores: list) -> None:
        """Log the results from a single iteration"""
        self.logger.info(
            f'... '
            f'instrument {self.instr}, '
            f'iteration {self.opt.get_numevals()}/{"?" if self.opt.get_maxeval() < 0 else self.opt.get_maxeval()}, '
            f'mean F: {round(np.nanmean(f_scores), 4)}, '
            f'stdev F: {round(np.nanstd(f_scores), 4)}, '
            f'{len(f_scores)} tracks ({len(cached_ids)} loaded from cache),'
        )

    def analyze_track(self, item: dict, **kwargs) -> dict:
        """Detect onsets in one track using a given combination of parameters."""
        # Create the onset detection maker class for this track
        made = OnsetMaker(item=item)
        # Create the onset envelope
        made.ons[self.instr] = made.onset_detect_cnn(
            self.instr,
            fps=self.fps,
            **kwargs    # We pass in all the arguments from our optimizer here,
        )
        # Return the results from this iteration, formatted as a dictionary
        return dict(
            track_name=item['track_name'],
            mbz_id=item['mbz_id'],
            fname=item['fname'],
            instrument=self.instr,
            f_score=self.get_f_score(onsetmaker=made),
            iterations=self.opt.get_numevals(),
            time=datetime.now().strftime("%d-%m-%y_%H-%M-%S"),
            **kwargs
        )


class OptimizeBeatTrackRNN(Optimizer):
    """Optimizes the `OnsetMaker.beat_track_rnn` function"""
    args = [
        ('threshold', float, 0, 1, 0.05),
        ('transition_lambda', float, 0, 500, 5),
        ('passes', int, 1, 5, 3),
    ]
    instr = 'mix'
    # If the track is longer than 1 minute, only use the first 60 seconds (reduces processing time)
    # TODO: we can probably remove this when running on the server
    audio_cutoff = 60
    # We can't use multithreading with MadMom, so this line ensures that joblib will process tracks linearly
    n_jobs = 1

    def __init__(self, items: dict, **kwargs):
        super().__init__(items, self.instr, self.args, **kwargs)
        self.correct: bool = kwargs.get('correct', True)
        self.csv_name: str = f'beat_track_{self.instr}'
        self.logger = self.enable_logger()
        try:
            self.cached_results = utils.load_csv(self.results_fpath, self.csv_name)
        except FileNotFoundError:
            pass

    def analyze_track(self, item: dict, **kwargs) -> dict:
        """Detect beats in one track using a given combination of parameters."""
        # Create the onset detection maker class for this track
        made = OnsetMaker(item=item)
        # Track the beats using recurrent neural networks
        timestamps, positions = made.beat_track_rnn(audio_cutoff=self.audio_cutoff, **kwargs)
        made.ons['mix'] = timestamps
        # Get the f score for beat positions
        f = self.get_f_score(onsetmaker=made)
        # Subset to get timestamps only for downbeats, then calculate F
        downbeat_ts = np.array([i1 for i1, i2 in zip(timestamps, positions) if i2 == 1])
        f_downbeats = made.compare_downbeats(downbeat_ts)['f_score']
        # Return the results from this iteration, formatted as a dictionary
        return dict(
            track_name=item['track_name'],
            mbz_id=item['mbz_id'],
            fname=item['fname'],
            instrument=self.instr,
            correct=self.correct,
            f_score=f,
            f_score_downbeats=f_downbeats,
            iterations=self.opt.get_numevals(),
            time=datetime.now().strftime("%d-%m-%y_%H-%M-%S"),
            **kwargs
        )

    def log_iteration(self, cached_ids: list, f_scores: list) -> None:
        """Log the results from a single iteration"""
        # We have to do this annoying logging thing to work with JobLib correctly...
        self.logger.info(
            f'... '
            f'instrument {self.instr}, '
            f'correct: {self.correct}, '
            f'iteration {self.opt.get_numevals()}/{"?" if self.opt.get_maxeval() < 0 else self.opt.get_maxeval()}, '
            f'mean F (beats): {round(np.nanmean(f_scores), 4)}, '
            f'stdev F (beats): {round(np.nanstd(f_scores), 4)}, '
            f'{len(f_scores)} tracks ({len(cached_ids)} loaded from cache),'
        )


def optimize_onset_detection_cnn(tracks: list[dict], **kwargs) -> None:
    """Central function for optimizing onset detection across all reference tracks and instrument stems

    Arguments:
        tracks (list[dict]): the metadata for the tracks to be used in optimization
        **kwargs: passed onto optimization class

    """

    def optimize_(instr_: str) -> None:
        o = OptimizeOnsetDetectCNN(
            items=tracks, instr=instr_, **kwargs
        )
        optimized_args, optimized_f_score = o.run_optimization()
        d = dict(
            instrument=o.instr,
            f_score=optimized_f_score,
            iterations=o.opt.get_numevals(),
            time=datetime.now().strftime("%d-%m-%y_%H-%M-%S"),
            **optimized_args
        )
        utils.save_csv(d, o.results_fpath, 'converged_parameters', )

    # Log the number of tracks we're optimizing
    logger = logging.getLogger(__name__)
    logger.info(f"optimising parameters across {len(tracks)} track/instrument combinations ...")
    # Get our combinations of instruments and filter parameters
    all_args = list(product(*[
        utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys(),   # instruments
    ]))
    # Optimize all combinations of parameters in series
    for args in all_args:
        optimize_(*args)


def optimize_beat_tracking(tracks: list[dict], **kwargs) -> None:
    """Central function for optimizing onset detection across all reference tracks and instrument stems

    Arguments:
        tracks (list[dict]): the metadata for the tracks to be used in optimization
        **kwargs: passed onto optimization class

    """
    def optimize_(correct_: bool = True,) -> None:
        o = OptimizeBeatTrackRNN(items=tracks, correct=correct_, **kwargs)
        optimized_args, optimized_f_score = o.run_optimization()
        d = dict(
            instrument=o.instr,
            correct=o.correct,
            f_score=optimized_f_score,
            iterations=o.opt.get_numevals(),
            time=datetime.now().strftime("%d-%m-%y_%H-%M-%S"),
            **optimized_args
        )
        utils.save_csv(d, o.results_fpath, 'converged_parameters_beat_track', )

    # Log the number of tracks we're optimizing
    logger = logging.getLogger(__name__)
    logger.info(f"optimising parameters across {len(tracks)} track/instrument combinations ...")
    # Get our combinations of instruments, center, and backtrack parameters
    all_args = list(product(*[
        [True],
        # [False, True],    # correct
    ]))
    # Optimize all combinations of parameters in parallel
    Parallel(n_jobs=1)(delayed(optimize_)(*args) for args in all_args)


@click.command()
@click.option(
    "-optimize_stems", "optimize_stems", is_flag=True, default=False,
    help='Optimize onset detection in given stems (e.g. piano, bass, drums)'
)
@click.option(
    "-optimize_mix", "optimize_mix", is_flag=True, default=True, help='Optimize beat detection in mixed audio'
)
@click.option(
    "-corpus", "corpus_fname", type=str, default='corpus_updated',
    help='The filename of the corpus to use when optimizing'
)
def main(
        optimize_stems: bool,
        optimize_mix: bool,
        corpus_fname: str
):
    """Run the onset detection procedure for the given corpus, using the given parameters"""
    # Configure the logger here
    logger = logging.getLogger(__name__)
    # Load in the results for tracks which have already been optimized
    to_optimise = utils.CorpusMaker.from_excel(fname=corpus_fname, only_30_corpus=False, only_annotated=True).tracks
    # Optimize stems
    if optimize_stems:
        stems = ", ".join(i for i in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys())
        logger.info(f'optimizing onset detection for {stems} ...')
        optimize_onset_detection_cnn(tracks=to_optimise)
        logger.info(f"... finished optimizing onset detection !")
    # Optimize beat tracking
    if optimize_mix:
        logger.info(f'optimizing beat detection for raw audio ...')
        optimize_beat_tracking(tracks=to_optimise)


if __name__ == '__main__':
    log_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_fmt)

    # not used in this stub but often useful for finding various files
    project_dir = Path(__file__).resolve().parents[2]

    # find .env automagically by walking up directories until it's found, then
    # load up the .env entries as environment variables
    load_dotenv(find_dotenv())

    main()
