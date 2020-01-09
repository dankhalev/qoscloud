#!/usr/bin/env python3
import logging
import time
from abc import ABC, abstractmethod
from collections import Callable
from os import path, makedirs
from threading import Thread
from typing import List, Optional

from pypapi import events as papi_events
from pypapi import papi_high
from pypapi.exceptions import PapiNoEventError

"""
Collects probes data and controls probe measurement process. The results are saved as files in `./probes/`
"""


class ProbeMonitor:

    def __init__(self, production: bool):
        self._workload_enabled = False
        self._probes: {str, Callable[[], None]} = {}
        self._production = production

    def execute_probe(self, probe_name: str, warm_up_cycles: int, measured_cycles: int,
                      cpu_events: Optional[List[str]] = None) -> int:
        assert not self._workload_enabled
        collector = DataCollector(probe_name, cpu_events, self._production)

        # Warm up
        executable = self._probes[probe_name]
        for _ in range(warm_up_cycles):
            executable()

        # Measured
        start = round(time.perf_counter() * 1000)
        for _ in range(measured_cycles):
            collector.before_iteration()
            executable()
            collector.after_iteration()
        collector.finish()

        return round(time.perf_counter() * 1000) - start

    @property
    def has_workload(self) -> bool:
        return self._workload_enabled

    def _workload(self, executable: "Callable[[], None]"):
        while self._workload_enabled:
            executable()

    def start_probe_workload(self, probe_name: str) -> None:
        assert not self._workload_enabled
        self._workload_enabled = True
        executable = self._probes[probe_name]
        self._workload_thread = Thread(target=self._workload,
                                       args=(executable,),
                                       name="Probe-WL")
        self._workload_thread.start()

    def stop_probe_workload(self) -> None:
        assert self._workload_enabled
        self._workload_enabled = False
        self._workload_thread.join()

    def add_probe(self, name: str, executable: "Callable[[], None]"):
        self._probes[name] = executable

    def has_probe(self, probe_name: str) -> bool:
        return probe_name in self._probes


class IterativeMonitor(ABC):

    @abstractmethod
    def before_iteration(self) -> None:
        pass

    @abstractmethod
    def after_iteration(self) -> None:
        pass

    @property
    @abstractmethod
    def header(self) -> List[str]:
        pass

    @property
    @abstractmethod
    def last_measurement(self) -> List[int]:
        pass

    def finish(self):
        pass


class TimeMonitor(IterativeMonitor):

    def __init__(self):
        self._start_time = 0
        self._end_time = 0

    def before_iteration(self) -> None:
        self._start_time = round(time.perf_counter() * 1000)

    def after_iteration(self) -> None:
        self._end_time = round(time.perf_counter() * 1000)

    @property
    def header(self) -> List[str]:
        return ["start_time", "end_time", "elapsed"]

    @property
    def last_measurement(self) -> List[int]:
        return [self._start_time, self._end_time, self._end_time - self._start_time]


class DiskMonitor(IterativeMonitor):
    DEFAULT_STAT_FILE_PATHS = ["/sys/block/sda/stat", "/sys/block/vda/stat"]
    DEFAULT_FEATURES = ["reads_completed", "reads_merged", "read_sectors", "read_time", "write_completed",
                        "write_merged", "written_sectors", "write_time", "io_in_progress", "io_time",
                        "weighted_io_time", "discards_completed", "discards_merged", "sectors_discarded",
                        "discards_time"]

    def __init__(self):
        # Find disk info file
        for file in self.DEFAULT_STAT_FILE_PATHS:
            if path.isfile(file):
                self._file = file

        # Check supported features
        with open(self._file, "r") as stream:
            self._num_features = len(stream.readline().split())
        # Warnings
        if self._num_features < len(self.DEFAULT_FEATURES):
            logging.warning(f"Recognized only {self._num_features} IO stats from {self.DEFAULT_FEATURES} supported")
        if self._num_features == 0:
            raise IOEventsNotSupportedException("Nothing to measure for IO")

        # Prepare data
        self._start_data = [0] * self._num_features
        self._end_data = [0] * self._num_features

    def before_iteration(self) -> None:
        with open(self._file, "r") as stream:
            self._start_data = [int(x) for x in stream.readline().split()]
        assert len(self._start_data) == self._num_features

    def after_iteration(self) -> None:
        with open(self._file, "r") as stream:
            self._end_data = [int(x) for x in stream.readline().split()]
        assert len(self._end_data) == self._num_features

    @property
    def header(self) -> List[str]:
        return self.DEFAULT_FEATURES[:self._num_features]

    @property
    def last_measurement(self) -> List[int]:
        return [end - start for start, end in zip(self._start_data, self._end_data)]


class CpuMonitor(IterativeMonitor):

    def __init__(self, cpu_events: List[str] = None):
        # Starts some counters
        # Check environment
        logging.info(f"CPU monitor supports {papi_high.num_counters()} counters in {papi_high.num_components()} "
                     f"components")
        if papi_high.num_counters() == 0:
            raise CPUEventsNotSupportedException("No CPU events to measure")
        # Events are defined at https://flozz.github.io/pypapi/events.html
        try:
            if cpu_events is None:
                papi_high.start_counters([
                    papi_events.PAPI_REF_CYC,
                    papi_events.PAPI_TOT_INS,
                    papi_events.PAPI_BR_INS,
                    papi_events.PAPI_L1_DCM
                    # CACHE-MISSES and CACHE-REFERENCES from perf missing in contrast to Java
                ])
                self._event_names = ["PAPI_REF_CYC", "PAPI_TOT_INS", "PAPI_BR_INS", "PAPI_L1_DCM"]
            else:
                assert len(cpu_events) > 0
                self._event_names = cpu_events
                cpu_events = [getattr(papi_events, event) for event in cpu_events]
                papi_high.start_counters(cpu_events)
        except (PapiNoEventError, AttributeError) as e:
            raise CPUEventsNotSupportedException(e)

    def before_iteration(self) -> None:
        # Reads values from counters and reset them
        papi_high.read_counters()

    def after_iteration(self) -> None:
        # Reads values from counters and reset them
        self._counters = papi_high.read_counters()

    @property
    def header(self) -> List[str]:
        return self._event_names

    @property
    def last_measurement(self) -> List[int]:
        return self._counters

    def finish(self):
        papi_high.stop_counters()


class DataCollector:
    SEPARATOR = ";"
    RESULTS_DIR = "./probes/"

    def __init__(self, probe_name: str, cpu_events: Optional[List[str]] = None, production: bool = False):
        # Monitors
        if not production:
            self._monitors: List[IterativeMonitor] = [TimeMonitor(), DiskMonitor(), CpuMonitor(cpu_events)]
        else:
            self._monitors: List[IterativeMonitor] = [TimeMonitor()]

        # Common header
        self._header: List[str] = []
        for monitor in self._monitors:
            self._header = self._header + monitor.header
        assert len(self._header) > 0

        # Prepare results folder
        if not path.exists(self.RESULTS_DIR):
            makedirs(self.RESULTS_DIR)

        # Prepare per probe files
        # Header
        with open(self.get_results_header_file(probe_name), "w") as header_file:
            header_file.write(str.join(self.SEPARATOR, self._header))
        # Data file
        self._data_file = open(self.get_results_data_file(probe_name), "w")

    @staticmethod
    def get_results_header_file(probe_name: str) -> str:
        return DataCollector.RESULTS_DIR + probe_name + ".header"

    @staticmethod
    def get_results_data_file(probe_name: str) -> str:
        return DataCollector.RESULTS_DIR + probe_name + ".data"

    @property
    def header(self) -> List[str]:
        return self._header

    def before_iteration(self) -> None:
        for monitor in self._monitors:
            monitor.before_iteration()

    def after_iteration(self) -> None:
        for monitor in self._monitors:
            monitor.after_iteration()

        data: List[int] = []
        for monitor in self._monitors:
            data = data + monitor.last_measurement
        assert len(data) == len(self._header)

        print(str.join(self.SEPARATOR, map(str, data)), file=self._data_file)

    def finish(self):
        for monitor in self._monitors:
            monitor.finish()

        self._data_file.close()


class IOEventsNotSupportedException(Exception):
    pass


class CPUEventsNotSupportedException(Exception):
    pass
