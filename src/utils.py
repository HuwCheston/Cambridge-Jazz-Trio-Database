#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Utility classes, functions, and variables used across the entire pipeline"""

import csv
import inspect
import json
import logging
import os
import pickle
import re
import time
from ast import literal_eval
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from string import punctuation
from tempfile import NamedTemporaryFile
from typing import Generator, Any

import audioread
import dill
import numpy as np
import pandas as pd
from tqdm import tqdm

# Set options in pandas and numpy here, so they carry through whenever this file is imported by another
# This disables scientific notation and forces all rows/columns to be printed: helps with debugging!
pd.set_option('display.max_rows', None)
pd.set_option('display.max_columns', None)
np.set_printoptions(suppress=True)

# Define variables used across many files
SAMPLE_RATE = 44100
HOP_LENGTH = 128
AUDIO_FILE_FMT = 'wav'
INSTRUMENTS_TO_PERFORMER_ROLES = {
    'piano': 'pianist',
    'bass': 'bassist',
    'drums': 'drummer'
}


def check_item_present_locally(fname: str) -> bool:
    """Returns whether a given filepath is present locally or not"""
    return os.path.isfile(os.path.abspath(fname))


def get_audio_duration(fpath: str) -> float:
    """Opens a given audio file and returns its duration"""

    try:
        with audioread.audio_open(fpath) as f:
            return float(f.duration)
    except FileNotFoundError:
        return 0.0


def disable_settingwithcopy_warning(func):
    """Simple decorator that disables the annoying SettingWithCopy warning in Pandas"""
    def wrapper(*args, **kwargs):
        pd.options.mode.chained_assignment = None
        res = func(*args, **kwargs)
        pd.options.mode.chained_assignment = None
        return res
    return wrapper


def remove_punctuation(s: str) -> str:
    """Removes punctuation from a string"""
    return s.translate(str.maketrans('', '', punctuation)).replace('’', '')


class CorpusMaker:
    """Converts a multi-sheet Excel spreadsheet into the required format for processing"""
    lbz_url_cutoff = 49
    json_indent = 4
    bandleader_instr = 'piano'

    def __init__(
            self,
            data: list[dict]
    ):
        self.tracks = list(self.format_track_dict(data))

    @classmethod
    def from_excel(
            cls,
            fname: str,
            ext: str = 'xlsx'
    ):
        """Construct corpus from an Excel spreadsheet, potentially containing multiple sheets"""
        realdata = []
        # These are the names of sheets that we don't want to process
        sheets_to_skip = ['notes', 'template', 'manual annotation']
        # Open the Excel file
        xl = pd.read_excel(pd.ExcelFile(fr'{get_project_root()}\references\{fname}.{ext}'), None, header=1).items()
        # Iterate through all sheets in the spreadsheet
        for sheet_name, trio in xl:
            if sheet_name.lower() not in sheets_to_skip:
                realdata.extend(cls.format_trio_spreadsheet(cls, trio))
        return cls(realdata)

    @classmethod
    def from_json(
            cls,
            fname: str,
            ext: str = 'json'
    ):
        """Construct corpus from a JSON"""
        # TODO: fill this in
        pass

    def __repr__(self):
        """Sets the string representation of this class to a DataFrame of all processed tracks"""
        return repr(pd.DataFrame(self.tracks))

    @disable_settingwithcopy_warning
    def format_trio_spreadsheet(
            self,
            trio_df: pd.DataFrame
    ) -> list[dict]:
        """Formats the spreadsheet for an individual trio and returns a list of dictionaries"""

        # We remove these columns from the dataframe
        to_drop = ['recording_id_for_lbz', 'recording_date_estimate', 'is_acceptable(Y/N)', 'link']
        # We rename these columns
        to_rename = {
            'piano': 'pianist',
            'bass': 'bassist',
            'drums': 'drummer',
            'release_title': 'album_name',
            'recording_title': 'track_name',
            'Unnamed: 19': 'notes',
            'Unnamed: 13': 'link'
        }
        # We keep these columns, in the following order
        to_keep = [
            'track_name',
            'album_name',
            'recording_year',
            'pianist',
            'bassist',
            'drummer',
            'youtube_link',
            'channel_overrides',
            'start_timestamp',
            'end_timestamp',
            'mbz_id',
            'notes',
            'time_signature',
            'first_downbeat'
        ]
        # Remove tracks that did not pass selection criteria
        sheet = trio_df[(trio_df['is_acceptable(Y/N)'] == 'Y') & (~trio_df['youtube_link'].isna())]
        # Strip punctuation from album and track name
        sheet['release_title'] = sheet['release_title'].apply(remove_punctuation)
        sheet['recording_title'] = sheet['recording_title'].apply(remove_punctuation)
        # Preserve the unique Musicbrainz ID for a track
        sheet['mbz_id'] = sheet['recording_id_for_lbz'].str[self.lbz_url_cutoff:]
        # Replace NA values in notes column with empty strings
        sheet = sheet.rename(columns={'Unnamed: 19': 'notes'})
        sheet['notes'] = sheet['notes'].fillna('')
        # Get the year the track was recorded in
        sheet['recording_year'] = pd.to_datetime(sheet['recording_date_estimate']).dt.year.astype(str)
        # Return the formatted dataframe, as a list of dictionaries
        return (
            sheet.rename(columns=to_rename)
            .drop(columns=to_drop)
            [to_keep]
            .reset_index(drop=True)
            .to_dict(orient='records')
        )

    @staticmethod
    def str_to_dict(s: str) -> dict:
        """Converts a string representation of a dictionary to a dictionary"""
        return {i.split(': ')[0]: i.split(': ')[1] for i in s.split(', ')}

    @staticmethod
    def format_timestamp(ts: str, as_string: bool = True):
        """Formats a timestamp string correctly. Returns as either a datetime or string, depending on `as_string`"""
        ts = str(ts)
        fmt = '%M:%S' if len(ts) < 6 else '%H:%M:%S'
        if as_string:
            return datetime.strptime(ts, fmt).strftime(fmt)
        else:
            return datetime.strptime(ts, fmt)

    def get_excerpt_duration(self, start, stop) -> str:
        """Returns the total duration of an excerpt, in the format %M:%S"""
        dur = (
                self.format_timestamp(stop, as_string=False) - self.format_timestamp(start, as_string=False)
        ).total_seconds()
        return str(timedelta(seconds=dur))[2:]

    @staticmethod
    def construct_filename(item, id_chars: int = 8, desired_words: int = 5) -> str:
        """Constructs the filename for an item in the corpus"""

        def name_formatter(st: str = "track_name") -> str:
            """Formats the name of a track or album by truncating to a given number of words, etc."""
            # Get the item name itself, e.g. album name, track name
            name = item[st].split(" ")
            # Get the number of words we require
            name_length = len(name) if len(name) < desired_words else desired_words
            return re.sub("[\W_]+", "", "".join(char.lower() for char in name[:name_length]))

        def musician_name_formatter(st: str) -> str:
            """Formats the name of a musician into the format: lastnamefirstinitial, e.g. Bill Evans -> evansb"""
            s = remove_punctuation(st).lower().split(" ")
            try:
                return s[1] + s[0][0]
            except IndexError:
                return 'musicianm'

        # Get the names of our musicians in the correct format
        pianist = musician_name_formatter(item["musicians"]["pianist"])
        bassist = musician_name_formatter(item["musicians"]["bassist"])
        drummer = musician_name_formatter(item["musicians"]["drummer"])
        # Get the required number of words of the track title, nicely formatted
        track = name_formatter("track_name")
        # Return our track name formatted nicely
        return rf"{pianist}-{track}-{bassist}{drummer}-{item['recording_year']}-{item['mbz_id'][:id_chars]}"

    def format_first_downbeat(
            self,
            start_ts: float,
            first_downbeat: float
    ) -> float:
        """Gets the position of the first downbeat in seconds, from the start of an excerpt"""
        start = self.format_timestamp(start_ts, as_string=False)
        start_td = timedelta(hours=start.hour, minutes=start.minute, seconds=start.second)
        return (timedelta(seconds=first_downbeat) - start_td).total_seconds()

    def format_track_dict(
            self,
            track_dict: dict
    ) -> Generator:
        """Formats each dictionary, corresponding to a single track"""

        to_drop = ['youtube_link', 'start_timestamp', 'end_timestamp', 'bassist', 'drummer',]
        # Iterate over every track in our list of dictionaries
        for track in track_dict:
            # Format the YouTube links correctly
            track['links'] = {'external': [l for l in [track['youtube_link']]]}
            # Get the total duration of the excerpt
            track['excerpt_duration'] = self.get_excerpt_duration(track['start_timestamp'], track['end_timestamp'])
            # Format our timestamps correctly
            track['timestamps'] = {
                'start': self.format_timestamp(track['start_timestamp']),
                'end': self.format_timestamp(track['end_timestamp'])
            }
            # Format our first downbeat using our start timestamp
            track['first_downbeat'] = self.format_first_downbeat(track['start_timestamp'], track['first_downbeat'])
            # Replace time signature with integer value
            track['time_signature'] = int(track['time_signature'])
            # Add an empty list for our log
            track['log'] = []
            # Format our musician names correctly
            track['musicians'] = {
                'pianist': track['pianist'],
                'bassist': track['bassist'],
                'drummer': track['drummer'],
                'leader': INSTRUMENTS_TO_PERFORMER_ROLES[self.bandleader_instr]
            }
            # Format our musician photos correctly
            track['photos'] = {
                "musicians": {
                    "pianist": None,
                    "bassist": None,
                    "drummer": None
                },
                "album_artwork": None
            }
            # Construct the filename for this track
            track['fname'] = self.construct_filename(track)
            # Format channel overrides as dictionary, or set key to empty dictionary if overrides are not present
            try:
                track['channel_overrides'] = self.str_to_dict(track['channel_overrides'])
            except AttributeError:
                track['channel_overrides'] = {}
            # Remove key-value pairs we no longer need
            for remove in to_drop:
                del track[remove]
            yield track


def get_project_root() -> Path:
    """Returns the root directory of the project"""
    return Path(__file__).absolute().parent.parent


def retry(exception, tries=4, delay=3, backoff=2):
    """Retry calling the decorated function using an exponential backoff."""
    def deco_retry(f):
        @wraps(f)
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 1:
                try:
                    return f(*args, **kwargs)
                except exception:
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return f(*args, **kwargs)
        return f_retry
    return deco_retry


def serialise_object(
        obj: object,
        fpath: str,
        fname: str,
        use_pickle: bool = False,
) -> None:
    """Wrapper around `dill.dump` that takes in an object, directory, and filename, and creates a serialised object"""
    if use_pickle:
        dumper = pickle.dump
    else:
        dumper = dill.dump
    with open(rf'{fpath}\{fname}.p', 'wb') as fi:
        dumper(obj, fi)


def unserialise_object(
        fpath: str,
        fname: str,
        use_pickle: bool = False
) -> object:
    """Simple wrapper around `dill.load` that unserialises an object and returns it"""
    if use_pickle:
        loader = pickle.load
    else:
        loader = dill.load
    return loader(open(fr'{fpath}\{fname}.p', 'rb'))


@retry(json.JSONDecodeError)
def load_json(
        fpath: str = 'r..\..\data\processed',
        fname: str = 'processing_results.json'
) -> dict:
    """Simple wrapper around `json.load` that catches errors when working on the same file in multiple threads"""
    with open(rf'{fpath}\{fname}.json', "r+") as in_file:
        return json.load(in_file)


def save_json(
        obj: dict,
        fpath: str,
        fname: str
) -> None:
    """Simple wrapper around `json.dump` with protections to assist in multithreaded access"""
    temp_file = NamedTemporaryFile(mode='w', dir=fpath, delete=False, suffix='.json')
    with temp_file as out_file:
        json.dump(obj, out_file, indent=4, default=str, )

    @retry(PermissionError)
    def replacer():
        os.replace(temp_file.name, rf'{fpath}\{fname}.json')

    replacer()


@retry(json.JSONDecodeError)
def load_csv(
        fpath: str = 'r..\..\data\processed',
        fname: str = 'processing_results'
) -> dict:
    """Simple wrapper around `json.load` that catches errors when working on the same file in multiple threads"""
    def eval_(i):
        try:
            return literal_eval(i)
        except (ValueError, SyntaxError) as _:
            return str(i)


    with open(rf'{fpath}\{fname}.csv', "r+") as in_file:
        return [{k: eval_(v) for k, v in row.items()} for row in csv.DictReader(in_file, skipinitialspace=True)]


def save_csv(
        obj: list | dict,
        fpath: str,
        fname: str
) -> None:
    """Simple wrapper around csv.DictWriter with protections to assist in multithreaded access"""
    # If we have an existing file with the same name, load it in and extend it with our new data
    try:
        existing_file = load_csv(fpath, fname)
    except FileNotFoundError:
        pass
    else:
        if isinstance(obj, dict):
            obj = [obj]
        obj = existing_file + obj

    # Create a new temporary file, in append mode
    temp_file = NamedTemporaryFile(mode='a', newline='', dir=fpath, delete=False, suffix='.csv')
    # Get our CSV header from the keys of the first dictionary, if we've passed in a list of dictionaries
    if isinstance(obj, list):
        keys = obj[0].keys()
    # Otherwise, if we've just passed in a dictionary, get the keys from it directly
    else:
        keys = obj.keys()
    # Open the temporary file and create a new dictionary writer with our given columns
    with temp_file as out_file:
        dict_writer = csv.DictWriter(out_file, keys)
        dict_writer.writeheader()
        # Write all the rows, if we've passed in a list
        if isinstance(obj, list):
            dict_writer.writerows(obj)
        # Alternatively, write a single row, if we've passed in a dictionary
        else:
            dict_writer.writerow(obj)

    @retry(PermissionError)
    def replacer():
        os.replace(temp_file.name, rf'{fpath}\{fname}.csv')

    replacer()


def try_get_kwarg_and_remove(
        kwarg: str,
        kwargs: dict,
        default_: bool = False
) -> Any:
    """Try and get an argument from a kwargs dictionary, remove after getting, and return the value (or a default).

    Arguments:
        kwarg (str): the argument to attempt to get from the kwargs dictionary
        kwargs (dict): the dictionary of keyword arguments
        default_ (bool, optional): the value to return if kwarg is not found in kwargs, defaults to None

    Returns:
        Any: the value returned from kwargs, or a default

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


class TqdmLoggingHandler(logging.Handler):
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg)
            self.flush()
        except Exception:
            self.handleError(record)


def return_function_kwargs(func) -> list:
    """Returns a list of keyword arguments accepted by a given function"""
    return [p for p in inspect.signature(func).parameters]


def iqr_filter(
        arr: np.array,
        low: int = 25,
        high: int = 75,
        mult: float = 1.5,
        fill_nans: bool = False,
) -> np.ndarray:
    """Simple IQR-based range filter that subsets array b where q1(b) - 1.5 * iqr(b) < b[n] < q3(b) + 1.5 * iqr(b)

    Parameters:
        arr (np.array): the array of values to clean
        low (int, optional): the lower quantile to use, defaults to 25
        high (int, optional): the upper quantile to use, defaults to 75
        mult (float, optional): the amount to multiply the IQR by, defaults to 1.5
        fill_nans (bool, optional): replace cleaned values with `np.nan`, such that the array shape remains the same

    Returns:
        np.array

    """
    # Get our upper and lower bound from the array
    min_ = np.nanpercentile(arr, low)
    max_ = np.nanpercentile(arr, high)
    # If the upper and lower bounds are equal, IQR will be 0.0, and our cleaned array will be empty. So don't clean.
    if min_ - max_ == 0:
        return arr
    # Construct the IQR
    iqr = max_ - min_
    # Filter the array between our two bounds and return the result
    if fill_nans:
        return np.array(
            [b if min_ - (mult * iqr) < b < max_ + (mult * iqr) else np.nan for b in arr]
        )
    else:
        return np.array(
            [b for b in arr if min_ - (mult * iqr) < b < max_ + (mult * iqr)]
        )


if __name__ == '__main__':
    pass