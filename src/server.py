import os
import sys
import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from src.tools.queue import (
    submit_task_handler,
    list_tasks_handler,
    get_task_handler,
    update_task_handler,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

QUEUE_DIR = os.environ.get("TASK_QUEUE_DIR", "/task-queue")


@asynccontextmanager
async def lifespan(app):
    if not os.path.isdir(QUEUE_DIR):
        logger.error(
            "TASK_QUEUE_DIR=%s does not exist or is not a directory — exiting.",
            QUEUE_DIR,
        )
        sys.exit(1)
    logger.info("task-queue-mcp started. Queue dir: %s", QUEUE_DIR)
    yield
    logger.info("task-queue-mcp shutting down.")


mcp = FastMCP("task-queue", lifespan=lifespan)


@mcp.tool()
def submit_task(
    source_agent: str,
    target_agent: str,
    task_type: str,
    summary: str,
    description: str,
    risk_level: str = "low",
    requires_approval: bool = False,
    priority: str = "normal",
    context_refs: list[str] = None,
    ttl_days: int = 30,
) -> dict:
    """
    Submit a new task to the queue.
    risk_level: low | medium | high
    priority: normal | high | urgent
    context_refs: list of absolute paths relevant to this task
    Returns: {ok, task_id, filename} on success or {ok: false, error} on failure.
    """
    return submit_task_handler(
        source_agent=source_agent,
        target_agent=target_agent,
        task_type=task_type,
        summary=summary,
        description=description,
        risk_level=risk_level,
        requires_approval=requires_approval,
        priority=priority,
        context_refs=context_refs or [],
        ttl_days=ttl_days,
        queue_dir=QUEUE_DIR,
    )


@mcp.tool()
def list_tasks(
    target_agent: str = None,
    source_agent: str = None,
    status: str = None,
    task_type: str = None,
    include_archived: bool = False,
    limit: int = 20,
) -> list:
    """
    List tasks from the queue with optional filters.
    status: single value or comma-separated (e.g. "submitted,approved")
    Returns tasks sorted by created descending. Expired tasks (past ttl_days) are excluded.
    """
    return list_tasks_handler(
        target_agent=target_agent,
        source_agent=source_agent,
        status=status,
        task_type=task_type,
        include_archived=include_archived,
        limit=limit,
        queue_dir=QUEUE_DIR,
    )


@mcp.tool()
def get_task(task_id: str) -> dict:
    """
    Get a task by UUID. Searches main queue then archive/.
    Returns full task dict or {ok: false, error}.
    """
    return get_task_handler(task_id=task_id, queue_dir=QUEUE_DIR)


@mcp.tool()
def update_task(
    task_id: str,
    status: str,
    actor: str,
    note: str = "",
    output: str = None,
) -> dict:
    """
    Update task status and append a history entry.
    Valid transitions: approved→in-progress, in-progress→completed, any non-terminal→failed.
    output is written to result.output on completed or failed.
    Returns {ok, task_id} or {ok: false, error}.
    """
    return update_task_handler(
        task_id=task_id,
        status=status,
        actor=actor,
        note=note,
        output=output,
        queue_dir=QUEUE_DIR,
    )


if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8485"))
    mcp.run(transport="streamable-http", host=host, port=port)
