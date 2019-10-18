# Copyright (c) 2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import time
from typing import Dict, List, Tuple, Optional

import re
import resource
from abc import abstractmethod
from dataclasses import dataclass

from wca import nodes, storage, platforms, profiling, perf_const as pc
from wca import resctrl
from wca import security
from wca.allocators import AllocationConfiguration
from wca.config import Numeric, Str
from wca.containers import ContainerManager, Container
from wca.detectors import TasksMeasurements, TasksResources, TasksLabels, TaskResource
from wca.logger import trace, get_logging_metrics, TRACE
from wca.metrics import Metric, MetricType, MetricName, MissingMeasurementException, \
    export_metrics_from_measurements
from wca.nodes import Task
from wca.nodes import TaskSynchronizationException
from wca.perf_pmu import UncorePerfCounters, _discover_pmu_uncore_imc_config, UNCORE_IMC_EVENTS, \
    PMUNotAvailable, UncoreDerivedMetricsGenerator
from wca.platforms import CPUCodeName
from wca.profiling import profiler
from wca.runners import Runner
from wca.storage import MetricPackage, DEFAULT_STORAGE

log = logging.getLogger(__name__)

_INITIALIZE_FAILURE_ERROR_CODE = 1

DEFAULT_EVENTS = (MetricName.INSTRUCTIONS, MetricName.CYCLES,
                  MetricName.CACHE_MISSES, MetricName.CACHE_REFERENCES, MetricName.MEMSTALL)


class TaskLabelGenerator:
    @abstractmethod
    def generate(self, task: Task) -> Optional[str]:
        """Generate new label value based on `task` object
        (e.g. based on other label value or one of task resource).
        `task` input parameter should not be modified."""
        ...


@dataclass
class TaskLabelRegexGenerator(TaskLabelGenerator):
    """Generate new label value based on other label value."""
    pattern: str
    repl: str
    source: str = 'task_name'  # by default use `task_name`

    def __post_init__(self):
        # Verify whether syntax for pattern and repl is correct.
        re.sub(self.pattern, self.repl, "")

    def generate(self, task: Task) -> Optional[str]:
        source_val = task.labels.get(self.source, None)
        if source_val is None:
            err_msg = "Source label {} not found in task {}".format(self.source, task.name)
            log.warning(err_msg)
            return None
        return re.sub(self.pattern, self.repl, source_val)


@dataclass
class TaskLabelResourceGenerator(TaskLabelGenerator):
    """Add label based on initial resource assignment of a task."""
    resource_name: str

    def generate(self, task: Task) -> Optional[str]:
        return str(task.resources.get(self.resource_name, "unknown"))


class MeasurementRunner(Runner):
    """MeasurementRunner run iterations to collect platform, resource, task measurements
    and store them in metrics_storage component.

    Arguments:
        node: component used for tasks discovery
        metrics_storage: storage to store platform, internal, resource and task metrics
            (defaults to DEFAULT_STORAGE/LogStorage to output for standard error)
        action_delay: iteration duration in seconds (None disables wait and iterations)
            (defaults to 1 second)
        rdt_enabled: enables or disabled support for RDT monitoring
            (defaults to None(auto) based on platform capabilities)
        extra_labels: additional labels attached to every metrics
            (defaults to empty dict)
        event_names: perf counters to monitor
            (defaults to instructions, cycles, cache-misses, memstalls)
        enable_derived_metrics: enable derived metrics ips, ipc and cache_hit_ratio
            (based on enabled_event names), default to False
        task_label_generators: component to generate additional labels for tasks
    """

    def __init__(
            self,
            node: nodes.Node,
            metrics_storage: storage.Storage = DEFAULT_STORAGE,
            action_delay: Numeric(0, 60) = 1.,  # [s]
            rdt_enabled: Optional[bool] = None,  # Defaults(None) - auto configuration.
            extra_labels: Dict[Str, Str] = None,
            event_names: List[str] = DEFAULT_EVENTS,
            enable_derived_metrics: bool = False,
            enable_perf_pmu: bool = True,
            task_label_generators: Dict[str, TaskLabelGenerator] = None,
            _allocation_configuration: Optional[AllocationConfiguration] = None,
            wss_reset_interval: int = 0,
    ):

        self._node = node
        self._metrics_storage = metrics_storage
        self._action_delay = action_delay
        self._rdt_enabled = rdt_enabled
        # Disabled by default, to be overridden by subclasses.
        self._rdt_mb_control_required = False
        # Disabled by default, to overridden by subclasses.
        self._rdt_cache_control_required = False
        # QUICK FIX for Str from ENV TODO: fix me
        self._extra_labels = {k: str(v) for k, v in
                              extra_labels.items()} if extra_labels else dict()
        self._finish = False  # Guard to stop iterations.
        self._last_iteration = time.time()  # Used internally by wait function.
        self._allocation_configuration = _allocation_configuration
        self._event_names = event_names
        log.info('Enabling %i perf events: %s', len(self._event_names),
                 ', '.join(self._event_names))
        self._enable_derived_metrics = enable_derived_metrics
        self._enable_perf_pmu = enable_perf_pmu

        # Default value for task_labels_generator.
        if task_label_generators is None:
            self._task_label_generators = {
                'application':
                    TaskLabelRegexGenerator('$', '', 'task_name'),
                'application_version_name':
                    TaskLabelRegexGenerator('.*$', '', 'task_name'),
            }
        else:
            self._task_label_generators = task_label_generators
        # Generate label value with cpu initial assignment, to simplify
        #   management of distributed model system for plugin:
        #   https://github.com/intel/platform-resource-manager/tree/master/prm"""
        #
        # To not risk subtle bugs in 1.0.x do not add it to _task_label_generators as default,
        #   but make it hardcoded here and possible do be removed.
        self._task_label_generators['initial_task_cpu_assignment'] = \
            TaskLabelResourceGenerator('cpus')

        self._wss_reset_interval = wss_reset_interval

        self._uncore_pmu = None
        self._write_to_cgroup = False

    @profiler.profile_duration(name='sleep')
    def _wait(self):
        """Decides how long one iteration should take.
        Additionally calculate residual time, based on time already taken by iteration.
        """
        now = time.time()
        iteration_duration = now - self._last_iteration

        residual_time = max(0., self._action_delay - iteration_duration)
        time.sleep(residual_time)
        self._last_iteration = time.time()

    def _initialize(self) -> Optional[int]:
        """Check RDT availability, privileges and prepare internal state.
        Can return error code that should stop Runner.
        """

        # Initialization (auto discovery Intel RDT features).
        rdt_available = resctrl.check_resctrl()
        if self._rdt_enabled is None:
            self._rdt_enabled = rdt_available
            log.info('RDT enabled (auto configuration): %s', self._rdt_enabled)
        elif self._rdt_enabled is True and not rdt_available:
            log.error('RDT explicitly enabled but not available - exiting!')
            return 1

        use_cgroup = self._write_to_cgroup
        use_resctrl = self._rdt_enabled
        use_perf = len(self._event_names) > 0

        if not security.are_privileges_sufficient(use_cgroup, use_resctrl, use_perf):
            return 1

        if self._rdt_enabled:
            # Resctrl is enabled and available, call a placeholder to allow further initialization.
            rdt_initialization_ok = self._initialize_rdt()
            if not rdt_initialization_ok:
                return 1

        log.debug('rdt_enabled: %s', self._rdt_enabled)
        platform, _, _ = platforms.collect_platform_information(self._rdt_enabled)
        rdt_information = platform.rdt_information

        self._event_names = _filter_out_event_names_for_cpu(
            self._event_names, platform.cpu_codename)

        # We currently do not support RDT without monitoring.
        if self._rdt_enabled and not rdt_information.is_monitoring_enabled():
            log.error('RDT monitoring is required - please enable CAT '
                      'or MBM with kernel parameters!')
            return 1

        self._containers_manager = ContainerManager(
            platform=platform,
            allocation_configuration=self._allocation_configuration,
            event_names=self._event_names,
            enable_derived_metrics=self._enable_derived_metrics,
            wss_reset_interval=self._wss_reset_interval,
        )

        self._init_uncore_pmu(self._enable_derived_metrics, self._enable_perf_pmu)

        return None

    def _init_uncore_pmu(self, enable_derived_metrics, enable_perf_pmu):
        self._uncore_pmu = None
        self._uncore_get_measurements = lambda: {}
        if enable_perf_pmu:
            try:
                cpus, pmu_events = _discover_pmu_uncore_imc_config(
                    UNCORE_IMC_EVENTS)
            except PMUNotAvailable as e:
                self._uncore_pmu = None
                self._uncore_get_measurements = lambda: {}
                log.warning('Perf pmu metrics requested, but not available. '
                            'Not collecting perf pmu metrics! '
                            'error={}'.format(e))
                return

            # Prepare uncore object
            self._uncore_pmu = UncorePerfCounters(
                cpus=cpus,
                pmu_events=pmu_events
            )

            # Wrap with derived..
            if enable_derived_metrics:
                self._uncore_derived_metrics = UncoreDerivedMetricsGenerator(
                    self._uncore_pmu.get_measurements)
                self._uncore_get_measurements = self._uncore_derived_metrics.get_measurements
            else:
                self._uncore_get_measurements = self._uncore_pmu.get_measurements

    def _iterate(self):
        iteration_start = time.time()

        # Get information about tasks.
        try:
            tasks = self._node.get_tasks()
        except TaskSynchronizationException as e:
            log.error('Cannot synchronize tasks with node (error=%s) - skip this iteration!', e)
            self._wait()
            return
        append_additional_labels_to_tasks(self._task_label_generators, tasks)
        log.debug('Tasks detected: %d', len(tasks))

        # Keep sync of found tasks and internally managed containers.
        containers = self._containers_manager.sync_containers_state(tasks)
        log.log(TRACE, 'Tasks container mapping:\n%s', '\n'.join(
            ['%s(%s)  =  %s' % (task.name, task.task_id, container._cgroup_path) for task, container
             in containers.items()]))

        # @TODO why not in platform module?
        extra_platform_measurements = self._uncore_get_measurements()

        # Platform information
        platform, platform_metrics, platform_labels = platforms.collect_platform_information(
            self._rdt_enabled, extra_platform_measurements=extra_platform_measurements)

        # Common labels
        common_labels = dict(platform_labels, **self._extra_labels)

        # Tasks data
        tasks_measurements, tasks_resources, tasks_labels = _prepare_tasks_data(containers)

        self._iterate_body(containers, platform, tasks_measurements, tasks_resources,
                           tasks_labels, common_labels)

        self._wait()

        iteration_duration = time.time() - iteration_start
        profiling.profiler.register_duration('iteration', iteration_duration)

        # Generic metrics.
        metrics_package = MetricPackage(self._metrics_storage)
        metrics_package.add_metrics(_get_internal_metrics(tasks))
        metrics_package.add_metrics(platform_metrics)
        metrics_package.add_metrics(_build_tasks_metrics(tasks_labels, tasks_measurements))
        metrics_package.add_metrics(profiling.profiler.get_metrics())
        metrics_package.add_metrics(get_logging_metrics())
        metrics_package.send(common_labels)

    def run(self) -> int:
        """Loop that gathers platform and tasks metrics and calls _iterate_body.
        _iterate_body is a method to be subclassed.
        """
        error_code = self._initialize()
        if error_code is not None:
            return error_code

        while True:
            self._iterate()

            if self._finish:
                break

        # Cleanup phase.
        self._containers_manager.cleanup()
        return 0

    def _iterate_body(self, containers, platform, tasks_measurements, tasks_resources,
                      tasks_labels, common_labels):
        """No-op implementation of inner loop body - called by iterate"""

    def _initialize_rdt(self) -> bool:
        """Nothing to configure in RDT to measure resource usage.
        Returns state of rdt initialization (True ok, False for error)
        """
        return True


def append_additional_labels_to_tasks(task_label_generators: Dict[str, TaskLabelGenerator],
                                      tasks: List[Task]) -> None:
    for task in tasks:
        # Add labels uniquely identifying a task.
        task.labels['task_id'] = task.task_id
        task.labels['task_name'] = task.name

        # Generate new labels based on formula inputted by a user (using TasksLabelGenerator).
        for target, task_label_generator in task_label_generators.items():
            if target in task.labels:
                err_msg = "Target label {} already existing in task {}. Skipping.".format(
                    target, task.name)
                log.debug(err_msg)
                continue
            val = task_label_generator.generate(task)
            if val is None:
                log.debug('Label {} for task {} not set, as its value is None.'
                          .format(target, task.name))
            else:
                if val == "":
                    log.debug('Label {} for task {} set to empty string.'.format(target, task.name))
                task.labels[target] = val


@profiler.profile_duration('prepare_tasks_data')
@trace(log, verbose=False)
def _prepare_tasks_data(containers: Dict[Task, Container]) -> \
        Tuple[TasksMeasurements, TasksResources, TasksLabels]:
    """Prepare all resource usage and resource allocation information and
    creates container-specific labels for all the generated metrics.
    """
    # Prepare empty structures for return all the information.
    tasks_measurements: TasksMeasurements = {}
    tasks_resources: TasksResources = {}
    tasks_labels: TasksLabels = {}

    for task, container in containers.items():
        # Task measurements and measurements based metrics.
        try:
            task_measurements = container.get_measurements()
        except MissingMeasurementException as e:
            log.warning('One or more measurements are missing '
                        'for container {} - ignoring! '
                        '(because {})'.format(container, e))
            continue
        # Extra metrics
        task_measurements[MetricName.UP.value] = 1
        task_measurements[MetricName.LAST_SEEN.value] = time.time()
        #
        if TaskResource.CPUS in task.resources:
            task_measurements[MetricName.CPUS.value] = task.resources[TaskResource.CPUS.value]
        if TaskResource.MEM in task.resources:
            task_measurements[MetricName.MEM.value] = task.resources[TaskResource.MEM.value]

        task_labels = task.labels.copy()

        # Aggregate over all tasks.
        tasks_labels[task.task_id] = task_labels
        tasks_measurements[task.task_id] = task_measurements
        tasks_resources[task.task_id] = task.resources

    return tasks_measurements, tasks_resources, tasks_labels


def _build_tasks_metrics(tasks_labels: TasksLabels,
                         tasks_measurements: TasksMeasurements) -> List[Metric]:
    """TODO:  TBD ALSO ADDS PREFIX for name!"""
    tasks_metrics: List[Metric] = []

    TASK_METRICS_PREFIX = 'task__'

    for task_id, task_measurements in tasks_measurements.items():
        task_metrics = export_metrics_from_measurements(TASK_METRICS_PREFIX, task_measurements)

        # Decorate metrics with task specific labels.
        for task_metric in task_metrics:
            task_metric.labels.update(tasks_labels[task_id])
        tasks_metrics += task_metrics
    return tasks_metrics


def _get_internal_metrics(tasks: List[Task]) -> List[Metric]:
    """Internal wca metrics e.g. memory usage, profiling information."""

    # Memory usage.
    memory_usage_rss_self = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    memory_usage_rss_children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    memory_usage_rss = memory_usage_rss_self + memory_usage_rss_children

    metrics = [
        Metric(name='wca_up', type=MetricType.COUNTER, value=time.time()),
        Metric(name='wca_tasks', type=MetricType.GAUGE, value=len(tasks)),
        Metric(name='wca_memory_usage_bytes', type=MetricType.GAUGE,
               value=int(memory_usage_rss * 1024)),
    ]

    return metrics


def _filter_out_event_names_for_cpu(
        event_names: List[str], cpu_codename: CPUCodeName) -> List[MetricName]:
    """Filter out events that cannot be collected on given cpu."""

    filtered_event_names = []

    for event_name in event_names:
        if event_name in pc.HardwareEventNameMap:
            # Universal metrics that works on all cpus.
            filtered_event_names.append(event_name)
        elif event_name in pc.PREDEFINED_RAW_EVENTS:
            if cpu_codename in pc.PREDEFINED_RAW_EVENTS[event_name]:
                filtered_event_names.append(event_name)
            else:
                log.warning('Event %r not supported for %s!', event_name, cpu_codename.value)
                continue
        elif '__r' in event_name:
            # Pass all raw events.
            filtered_event_names.append(event_name)
        else:
            raise Exception('Unknown event name %r!' % event_name)

    return filtered_event_names
