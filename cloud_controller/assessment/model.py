#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
This classes store apps architecture for assessment and avocadoctl
"""
import string
from enum import IntEnum
from threading import Lock
from typing import Dict, List, Optional, Tuple

import random

import cloud_controller.architecture_pb2 as arch_pb
from cloud_controller.assessment import RESULTS_PATH
from cloud_controller.knowledge.model import Application, Probe
from cloud_controller.analysis.predictor_interface import predictor_pb2


class AppStatus(IntEnum):
    RECEIVED = 1
    REJECTED = 2
    ACCEPTED = 3
    PUBLISHED = 4


class AppEntry:
    def __init__(self, architecture: arch_pb.Architecture):
        self.architecture = architecture
        self._name: str = architecture.name
        self.status = AppStatus.RECEIVED

    @property
    def name(self) -> str:
        return self._name


class AppDatabase:
    def __init__(self):
        self._apps: Dict[str, AppEntry] = {}
        self._update_lock = Lock()
        self._app_removal_cache: List[str] = []
        self.probes_by_alias: Dict[str, Probe] = {}

    def __contains__(self, item: object) -> bool:
        return self._apps.__contains__(item)

    def __getitem__(self, app_name: str) -> AppEntry:
        return self._apps[app_name]

    def _generate_alias(self):
        id_ = ''.join(random.choice(string.ascii_uppercase) for _ in range(4))
        while id_ in self.probes_by_alias:
            id_ = ''.join(random.choice(string.ascii_uppercase) for _ in range(4))
        self.probes_by_alias[id_] = None
        return id_


    def add_app(self, architecture: arch_pb.Architecture) -> None:
        with self._update_lock:
            for component in architecture.components:
                for probe in architecture.components[component].probes:
                    assert probe.alias == ""
                    probe.alias = self._generate_alias()
            entry = AppEntry(architecture)
            self._apps[architecture.name] = entry

    def remove_app(self, name: str) -> None:
        with self._update_lock:
            status: AppStatus = self._apps[name].status

            if status is AppStatus.PUBLISHED:
                # Publish the removal to other parts of framework
                self._app_removal_cache.append(name)

            # Delete app from db
            del self._apps[name]

    def publish_new_architectures(self) -> List[arch_pb.Architecture]:
        architectures: List[arch_pb.Architecture] = []

        with self._update_lock:
            # Foreach app
            for app in self._apps.values():
                if app.status == AppStatus.ACCEPTED:
                    # Append to list
                    architectures.append(app.architecture)

                    # Update app status
                    app.status = AppStatus.PUBLISHED
                    app.architecture = None

        return architectures

    def publish_new_removals(self) -> List[str]:
        with self._update_lock:
            tmp = self._app_removal_cache
            self._app_removal_cache = []
        return tmp

    def update_app_status(self, app_name: str, new_status: AppStatus) -> None:
        with self._update_lock:
            self._apps[app_name].status = new_status

    def get_app_status(self, app_name: str) -> Optional[AppStatus]:
        with self._update_lock:
            if app_name in self._apps:
                return self._apps[app_name].status
            else:
                return None

    def print_stats(self, app_name: str) -> str:
        # Basic info
        status = "App name: %s\nApp status: %s\n" % (app_name, str(self._apps[app_name].status))
        return status

probe_aliases = set()


class Scenario:
    def __init__(self, controlled_probe: Probe, background_probes: List[Probe], hw_id: str, warm_up_cycles: int = 100,
                 measured_cycles: int = 400, cpu_events=None):
        self.controlled_probe = controlled_probe
        self.background_probes = background_probes
        self.hw_id = hw_id
        self.warm_up_cycles = warm_up_cycles
        self.measured_cycles = measured_cycles
        if cpu_events is None:
            self.cpu_events = []  # ["JVM:compilations"]
        else:
            self.cpu_events = cpu_events
        self._id: str = None
        self.filename_header, self.filename_data = Scenario.get_results_path(self)

    @staticmethod
    def get_folder(probe: Probe, hw_config: str) -> str:
        return RESULTS_PATH + "/" + probe.component.application.name + "/" + hw_config + "/"

    @staticmethod
    def _get_fs_probe_name(probe: Probe) -> str:
        # TODO
        return probe.alias  # f"{probe.component.name}_{probe.name}"

    @staticmethod
    def get_results_path(scenario: "Scenario") -> Tuple[str, str]:
        """
        Returns path to header and data file for selected scenario
        """
        folder = Scenario.get_folder(scenario.controlled_probe, scenario.hw_id)
        file = "merged_iterative_result--batch--" + \
               "-".join(Scenario._get_fs_probe_name(probe)
                        for probe in [scenario.controlled_probe] + scenario.background_probes)
        path = folder + '/' + file
        return path + ".header", path + ".out"

    @property
    def id_(self) -> str:
        return self._id

    @id_.setter
    def id_(self, id_: str):
        self._id = id_

    @staticmethod
    def init_from_pb(scenario_pb: predictor_pb2.Scenario, applications: Dict[str, Application]) -> "Scenario":
        """
        Creates a probe object from protobuf representation.
        """
        controlled = Probe.init_from_pb(scenario_pb.controlled_probe, applications)
        background: List[Probe] = []
        for probe_pb in scenario_pb.background_probes:
            background.append(Probe.init_from_pb(probe_pb, applications))
        scenario = Scenario(
            controlled_probe=controlled,
            background_probes=background,
            hw_id=scenario_pb.hw_id,
            warm_up_cycles=scenario_pb.warm_up_cycles,
            measured_cycles=scenario_pb.measured_cycles,
            cpu_events=scenario_pb.cpu_events
        )
        scenario.id_ = scenario_pb.id
        return scenario

    def pb_representation(self, scenario_pb):
        scenario_pb.hw_id = self.hw_id
        scenario_pb.measured_cycles = self.measured_cycles
        scenario_pb.warm_up_cycles = self.warm_up_cycles
        scenario_pb.cpu_events = self.cpu_events
        scenario_pb.controlled_probe.name = self.controlled_probe.name
        scenario_pb.controlled_probe.application = self.controlled_probe.component.application.name
        scenario_pb.controlled_probe.component = self.controlled_probe.component.name
        scenario_pb.controlled_probe.time_limit = self.controlled_probe.time_limit
        scenario_pb.filename = self.filename_data
        for bg_probe in self.background_probes:
            probe_pb = scenario_pb.background_probes.add()
            probe_pb.name = bg_probe.name
            probe_pb.component = bg_probe.component.name
            probe_pb.application = bg_probe.component.application.name
            probe_pb.time_limit = bg_probe.time_limit
        return scenario_pb

    def __str__(self) -> str:
        # Main component
        msg = "%s [" % self.controlled_probe

        # Background components
        msg += ", ".join(str(probe) for probe in self.background_probes)
        msg += "]"

        return msg