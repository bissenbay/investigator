#!/usr/bin/env python3
# thoth-investigator
# Copyright(C) 2020 Francesco Murdaca
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""This file contains methods used by Thoth investigator to investigate on unresolved packages."""

import sys
import logging
import json
import os

from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

from thoth.storages.graph import GraphDatabase
from thoth.messaging import MessageBase
from thoth.messaging import UnresolvedPackageMessage
from thoth.common import OpenShift
from thoth.python import Pipfile
from thoth.python import Source

from ..metrics import scheduled_workflows
from .. import common
from .metrics_unresolved_package import unresolved_package_exceptions
from .metrics_unresolved_package import unresolved_package_in_progress
from .metrics_unresolved_package import unresolved_package_success

_LOG_SOLVER = os.environ.get("THOTH_LOG_SOLVER") == "DEBUG"

_LOGGER = logging.getLogger(__name__)


def investigate_unresolved_package(file_test_path: Optional[Path] = None) -> Tuple[Dict[Any, Any], Optional[str]]:
    """Investigate on unresolved packages."""
    if file_test_path:
        _LOGGER.debug("Dry run..")
        adviser_run_path = file_test_path
    else:
        adviser_run_path = Path(os.environ["JSON_FILE_PATH"])

    if not adviser_run_path.exists():
        raise FileNotFoundError(f"Cannot find the file on this path: {adviser_run_path}")

    with open(adviser_run_path, "r") as f:
        content = json.load(f)

    unresolved_packages = []
    report = content["result"]["report"]
    if report:
        errors_details = report.get("_ERROR_DETAILS")
        if errors_details:
            unresolved_packages = errors_details["unresolved"]

    if not unresolved_packages:
        _LOGGER.warning("No packages to be solved with priority identified.")
        sys.exit(2)

    parameters = content["result"]["parameters"]
    runtime_environment = parameters["project"].get("runtime_environment")

    solver = OpenShift.obtain_solver_from_runtime_environment(runtime_environment=runtime_environment)

    requirements = parameters["project"].get("requirements")

    pipfile = Pipfile.from_dict(requirements)
    packages = pipfile.packages.packages
    dev_packages = pipfile.dev_packages.packages

    packages_to_solve = {}
    for package_name in unresolved_packages:

        if package_name in packages:
            packages_to_solve[package_name] = packages[package_name]

        if package_name in dev_packages:
            packages_to_solve[package_name] = dev_packages[package_name]

    _LOGGER.info(f"Unresolved packages identified.. {packages_to_solve}")

    if solver:
        return (packages_to_solve, solver)

    return (packages_to_solve, None)


@unresolved_package_exceptions.count_exceptions()
@unresolved_package_in_progress.track_inprogress()
def parse_unresolved_package_message(
    unresolved_package: MessageBase, openshift: OpenShift, graph: GraphDatabase
) -> None:
    """Parse unresolved package message."""
    package_name = unresolved_package.package_name
    package_version = unresolved_package.package_version
    requested_indexes: Optional[List[str]] = unresolved_package.index_url
    solver = unresolved_package.solver

    total_solver_wfs_scheduled = 0
    total_revsolver_wfs_scheduled = 0
    total_si_wfs_scheduled = 0

    # Select indexes
    registered_indexes: List[str] = graph.get_python_package_index_urls_all()

    indexes = registered_indexes

    if not requested_indexes:
        _LOGGER.info("Using Thoth registered indexes...")
    else:
        if all(index_url in registered_indexes for index_url in requested_indexes):
            indexes = requested_indexes
            _LOGGER.info("Using requested indexes...")
        else:
            _LOGGER.info("Using Thoth registered indexes...")

    # Parse package version for each index
    for index_url in indexes:

        versions = _check_package_version(
            package_name=package_name, package_version=package_version, index_url=index_url
        )

        revsolver_packages_seen: List[Tuple[str, str]] = []

        # Loop versions from the latest one
        for version in versions:

            # Check if package version index exists in Thoth Knowledge Graph
            is_present = graph.python_package_version_exists(
                package_name=package_name, package_version=version, index_url=index_url
            )

            # Solver logic

            solver_wfs_scheduled = learn_using_solver(
                openshift=openshift,
                graph=graph,
                is_present=is_present,
                package_name=package_name,
                package_version=version,
                index_url=index_url,
                solver=solver,
            )

            # Revsolver logic

            revsolver_wfs_scheduled, revsolver_packages_seen = common.learn_using_revsolver(
                openshift=openshift,
                is_present=is_present,
                package_name=package_name,
                package_version=version,
                revsolver_packages_seen=revsolver_packages_seen,
            )

            # SI logic

            si_wfs_scheduled = common.learn_about_security(
                openshift=openshift,
                graph=graph,
                is_present=is_present,
                package_name=package_name,
                package_version=version,
                index_url=index_url,
            )

            total_solver_wfs_scheduled += solver_wfs_scheduled
            total_revsolver_wfs_scheduled += revsolver_wfs_scheduled
            total_si_wfs_scheduled += si_wfs_scheduled

    scheduled_workflows.labels(message_type=UnresolvedPackageMessage.topic_name, workflow_type="solver").inc(
        total_solver_wfs_scheduled
    )

    scheduled_workflows.labels(message_type=UnresolvedPackageMessage.topic_name, workflow_type="revsolver").inc(
        total_revsolver_wfs_scheduled
    )

    scheduled_workflows.labels(
        message_type=UnresolvedPackageMessage.topic_name, workflow_type="security-indicator"
    ).inc(total_si_wfs_scheduled)

    unresolved_package_success.inc()


def _check_package_version(package_name: str, package_version: Optional[str], index_url: str) -> List[str]:
    """Check package version."""
    versions = []

    if not package_version or package_version == "*":
        _LOGGER.debug("consider index %r", index_url)
        source = Source(index_url)

        try:
            # Get sorted versions (latest first -> latest = versions[0])
            versions = list(map(str, source.get_sorted_package_versions(package_name)))

        except Exception as exc:
            _LOGGER.warning(
                f"Could not retrieve versions for package {package_name} from index {index_url}: {str(exc)}"
            )

    else:
        versions.append(package_version)

    return versions


def learn_using_solver(
    openshift: OpenShift,
    graph: GraphDatabase,
    is_present: bool,
    package_name: str,
    index_url: str,
    package_version: str,
    solver: Optional[str],
) -> int:
    """Learn using solver about Package Version Index dependencies."""
    if not is_present:
        # Package never seen (schedule all solver workflows to collect all knowledge for Thoth)
        are_solvers_scheduled = _schedule_all_solvers(
            openshift=openshift, package_name=package_name, package_version=package_version, indexes=[index_url]
        )
        return are_solvers_scheduled

    # Select solvers
    if not solver:
        solvers: List[str] = openshift.get_solver_names()
    else:
        solvers = [solver]

    # Check for which solver has not been solved and schedule solver workflow
    are_solvers_scheduled = 0

    for solver_name in solvers:
        is_solved = graph.python_package_version_exists(
            package_name=package_name, package_version=package_version, index_url=index_url, solver_name=solver_name
        )

        if not is_solved:

            is_solver_scheduled = _schedule_solver(
                openshift=openshift,
                package_name=package_name,
                package_version=package_version,
                indexes=[index_url],
                solver_name=solver_name,
            )

            are_solvers_scheduled += is_solver_scheduled

    return are_solvers_scheduled


def _schedule_solver(
    openshift: OpenShift, package_name: str, package_version: str, indexes: List[str], solver_name: str
) -> int:
    """Schedule solver."""
    try:
        packages = f"{package_name}==={package_version}"

        analysis_id = openshift.schedule_solver(
            solver=solver_name, packages=packages, indexes=indexes, transitive=False, debug=_LOG_SOLVER
        )
        _LOGGER.info(
            "Scheduled solver %r for packages %r from indexes %r, analysis is %r",
            solver_name,
            packages,
            indexes,
            analysis_id,
        )
        is_scheduled = 1
    except Exception as e:
        _LOGGER.exception(f"Failed to schedule solver {solver_name} for package {packages} from {indexes}: {e}")
        is_scheduled = 0

    return is_scheduled


def _schedule_all_solvers(openshift: OpenShift, package_name: str, package_version: str, indexes: List[str]) -> int:
    """Schedule all solvers."""
    try:
        packages = f"{package_name}==={package_version}"

        analysis_ids = openshift.schedule_all_solvers(packages=packages, indexes=indexes)
        _LOGGER.info(
            "Scheduled solvers %r for packages %r from indexes %r, analysis ids are %r", packages, indexes, analysis_ids
        )
        are_scheduled = len(analysis_ids)
    except Exception as e:
        _LOGGER.exception(f"Failed to schedule solvers for package {packages} from {indexes}: {e}")
        are_scheduled = 0

    return are_scheduled
