"""MongoDB based manager for jobflow."""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    import jobflow


def upload_flow(
    flow: jobflow.Flow | jobflow.Job | list[jobflow.Job],
    tasks_store: jobflow.JobStore = None,
    job_store: jobflow.JobStore = None,
    allow_external_references: bool = False,
) -> None:
    """Upload a flow to a tasks store."""


def run_flow(
    tasks_store: jobflow.JobStore = None,
    job_store: jobflow.JobStore = None,
    uuid: str | None = None,
) -> None:
    """
    Run a flow from a tasks store.

    Parameters
    ----------
    tasks_store
        A tasks store. Alternatively, if set to None, :obj:`JobflowSettings.TASKS_STORE`
        will be used.
    job_store
        A job store. Alternatively, if set to None, :obj:`JobflowSettings.JOB_STORE`
        will be used. Note, this could be different on the computer that submits the
        workflow and the computer which runs the workflow. The value of ``JOB_STORE``
        on the computer that runs the workflow will be used.
    uuid
        The uuid of the flow to run. If None, the latest flow will be run.
    """
