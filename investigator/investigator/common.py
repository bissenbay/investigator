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


"""This is Thoth investigator common methods."""

import os
import logging
from urllib.parse import urlparse

from typing import List, Tuple

from thoth.common import OpenShift
from thoth.storages import GraphDatabase
from thoth.sourcemanagement.sourcemanagement import SourceManagement
from thoth.sourcemanagement.enums import ServiceType

_LOGGER = logging.getLogger(__name__)

_LOG_REVSOLVER = os.environ.get("THOTH_LOG_REVSOLVER") == "DEBUG"
GITHUB_PRIVATE_TOKEN = os.getenv("THOTH_GITHUB_PRIVATE_TOKEN")
GITLAB_PRIVATE_TOKEN = os.getenv("THOTH_GITLAB_PRIVATE_TOKEN")


def learn_about_security(
    openshift: OpenShift,
    graph: GraphDatabase,
    is_present: bool,
    package_name: str,
    index_url: str,
    package_version: str,
) -> int:
    """Learn about security of Package Version Index."""
    if is_present:
        # Check if package version index has been already analyzed for security
        is_analyzed = graph.si_aggregated_python_package_version_exists(
            package_name=package_name, package_version=package_version, index_url=index_url
        )

        if is_analyzed:
            return 0

    # Package never seen (schedule si workflow to collect knowledge for Thoth)
    is_si_analyzer_scheduled = _schedule_security_indicator(
        openshift=openshift, package_name=package_name, package_version=package_version, index_url=index_url
    )

    return is_si_analyzer_scheduled


def _schedule_security_indicator(openshift: OpenShift, package_name: str, package_version: str, index_url: str) -> int:
    """Schedule Security Indicator."""
    try:
        analysis_id = openshift.schedule_security_indicator(
            python_package_name=package_name,
            python_package_version=package_version,
            python_package_index=index_url,
            aggregation_function="process_data",
        )
        _LOGGER.info(
            "Scheduled SI %r for package %r in version %r from index %r, analysis is %r",
            package_name,
            package_version,
            index_url,
            analysis_id,
        )
        is_scheduled = 1
    except Exception as e:
        _LOGGER.exception(
            f"Failed to schedule SI for package {package_name} in version {package_version} from index {index_url}: {e}"
        )
        is_scheduled = 0

    return is_scheduled


def learn_using_revsolver(
    openshift: OpenShift,
    is_present: bool,
    package_name: str,
    package_version: str,
    revsolver_packages_seen: List[Tuple[str, str]],
) -> Tuple[int, List[Tuple[str, str]]]:
    """Learn using revsolver about Package Version dependencies."""
    if not is_present and (package_name, package_version) not in revsolver_packages_seen:
        # Package never seen (schedule revsolver workflow to collect knowledge for Thoth)
        is_revsolver_scheduled = _schedule_revsolver(
            openshift=openshift, package_name=package_name, package_version=package_version
        )
        revsolver_packages_seen.append((package_name, package_version))

        return is_revsolver_scheduled, revsolver_packages_seen

    return 0, revsolver_packages_seen


def _schedule_revsolver(openshift: OpenShift, package_name: str, package_version: str) -> int:
    """Schedule revsolver."""
    try:
        analysis_id = openshift.schedule_revsolver(
            package_name=package_name, package_version=package_version, debug=_LOG_REVSOLVER
        )
        _LOGGER.info(
            "Scheduled reverse solver for package %r in version %r, analysis is %r",
            package_name,
            package_version,
            analysis_id,
        )
        is_scheduled = 1
    except Exception as e:
        _LOGGER.exception(
            "Failed to schedule reverse solver for %r in version %r: %r", package_name, package_version, e
        )
        is_scheduled = 0

    return is_scheduled


def git_source_from_url(url: str) -> SourceManagement:
    """Parse URL to get SourceManagement object."""
    res = urlparse(url)
    service_url = res.netloc
    service_name = service_url.split(".")[-2]
    service_type = ServiceType.by_name(service_name)
    if service_type == ServiceType.GITHUB:
        token = GITHUB_PRIVATE_TOKEN
    elif service_type == ServiceType.GITLAB:
        token = GITLAB_PRIVATE_TOKEN
    else:
        raise NotImplementedError("There is no token for this service type")
    return SourceManagement(service_type, res.scheme + "://" + res.netloc, token, res.path)
