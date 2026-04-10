# -*- coding: utf-8 -*-
"""MultiAgentManager: Manages multiple agent workspaces with lazy loading.

Provides centralized management for multiple Workspace objects,
including lazy loading, lifecycle management, and hot reloading.
"""
import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from .workspace import Workspace
from ..config.utils import (
    copaw_storage_isolation_enabled,
    get_config_path,
    load_config,
)

logger = logging.getLogger(__name__)


def _workspace_cache_key(config_path: Path, agent_id: str) -> Tuple[str, str]:
    """Stable key for (tenant config file, agent id)."""
    return (str(config_path.expanduser().resolve()), agent_id)


class MultiAgentManager:
    """Manages multiple agent workspaces.

    Features:
    - Lazy loading: Workspaces are created only when first requested
    - Lifecycle management: Start, stop, reload workspaces
    - Thread-safe: Uses async lock for concurrent access
    - Hot reload: Reload individual workspaces without affecting others
    """

    def __init__(self):
        """Initialize multi-agent manager."""
        self.agents: Dict[Tuple[str, str], Workspace] = {}
        self._lock = asyncio.Lock()
        self._cleanup_tasks: Set[asyncio.Task] = set()
        logger.debug("MultiAgentManager initialized")

    async def get_agent(
        self,
        agent_id: str,
        config_path: Optional[Path] = None,
    ) -> Workspace:
        """Get agent workspace by ID (lazy loading).

        If workspace doesn't exist in memory, it will be created and started.
        Thread-safe using async lock.

        Args:
            agent_id: Agent ID to retrieve
            config_path: Root ``config.json`` for agent profiles; default global.

        Returns:
            Workspace: The requested workspace instance

        Raises:
            ValueError: If agent ID not found in configuration
        """
        if config_path is None:
            config_path = get_config_path()
        cache_key = _workspace_cache_key(config_path, agent_id)

        async with self._lock:
            if cache_key in self.agents:
                logger.debug(
                    "Returning cached agent: %s scope=%s...",
                    agent_id,
                    cache_key[0][:40],
                )
                return self.agents[cache_key]

            config = load_config(config_path)

            if agent_id not in config.agents.profiles:
                raise ValueError(
                    f"Agent '{agent_id}' not found in configuration. "
                    f"Available agents: {list(config.agents.profiles.keys())}",
                )

            agent_ref = config.agents.profiles[agent_id]

            logger.info(f"Creating new workspace: {agent_id}")
            instance = Workspace(
                agent_id=agent_id,
                workspace_dir=agent_ref.workspace_dir,
                root_config_path=config_path,
            )

            try:
                await instance.start()
                instance.set_manager(self)
                self.agents[cache_key] = instance
                logger.info(f"Workspace created and started: {agent_id}")
                return instance
            except Exception as e:
                logger.error(f"Failed to start workspace {agent_id}: {e}")
                raise

    async def _graceful_stop_old_instance(
        self,
        old_instance: Workspace,
        agent_id: str,
    ) -> None:
        """Gracefully stop old instance after checking for active tasks.

        If active tasks exist, schedule delayed cleanup in background.
        Otherwise, stop immediately.

        Args:
            old_instance: The old workspace instance to stop
            agent_id: Agent ID for logging
        """
        has_active = await old_instance.task_tracker.has_active_tasks()

        if has_active:
            # Active tasks - schedule delayed cleanup in background
            active_tasks = await old_instance.task_tracker.list_active_tasks()
            logger.info(
                f"Old workspace instance has {len(active_tasks)} active "
                f"task(s): {active_tasks}. Scheduling delayed cleanup for "
                f"{agent_id}.",
            )

            async def delayed_cleanup():
                """Wait for tasks to complete, then stop old instance."""
                try:
                    # Wait up to 1 minutes for tasks to complete
                    completed = await old_instance.task_tracker.wait_all_done(
                        timeout=60.0,
                    )
                    if completed:
                        logger.info(
                            f"All tasks completed for old instance "
                            f"{agent_id}. Stopping now.",
                        )
                    else:
                        logger.warning(
                            f"Timeout waiting for tasks to complete for "
                            f"{agent_id}. Forcing stop after 5 minutes.",
                        )

                    await old_instance.stop(final=False)
                    logger.info(
                        f"Old workspace instance stopped: {agent_id}. "
                        f"Delayed cleanup completed.",
                    )
                except Exception as e:
                    logger.warning(
                        f"Error during delayed cleanup for {agent_id}: {e}. "
                        f"New instance is serving requests.",
                    )

            # Create background task for delayed cleanup and track it
            cleanup_task = asyncio.create_task(delayed_cleanup())
            self._cleanup_tasks.add(cleanup_task)

            def _on_cleanup_done(task: asyncio.Task) -> None:
                """Remove task from tracking set and log errors."""
                self._cleanup_tasks.discard(task)
                if task.cancelled():
                    logger.info(
                        f"Delayed cleanup task for {agent_id} was cancelled.",
                    )
                    return
                exc = task.exception()
                if exc is not None:
                    logger.warning(
                        f"Error in delayed cleanup task for {agent_id}: "
                        f"{exc}.",
                    )

            cleanup_task.add_done_callback(_on_cleanup_done)
            logger.info(
                f"Zero-downtime reload completed: {agent_id}. "
                f"Old instance cleanup scheduled in background.",
            )
        else:
            # No active tasks - stop immediately
            logger.debug(
                f"No active tasks in old instance {agent_id}. "
                f"Stopping immediately.",
            )
            try:
                await old_instance.stop(final=False)
                logger.info(
                    f"Old workspace instance stopped: {agent_id}. "
                    f"Zero-downtime reload completed.",
                )
            except Exception as e:
                logger.warning(
                    f"Failed to stop old workspace instance for "
                    f"{agent_id}: {e}. "
                    f"New instance is active and serving requests.",
                )

    async def stop_agent(
        self,
        agent_id: str,
        config_path: Optional[Path] = None,
    ) -> bool:
        """Stop a specific agent instance.

        Args:
            agent_id: Agent ID to stop
            config_path: Config scope; default global ``get_config_path()``.

        Returns:
            bool: True if agent was stopped, False if not running
        """
        if config_path is None:
            config_path = get_config_path()
        cache_key = _workspace_cache_key(config_path, agent_id)

        async with self._lock:
            if cache_key not in self.agents:
                logger.warning(f"Agent not running: {agent_id}")
                return False

            instance = self.agents[cache_key]
            await instance.stop()
            del self.agents[cache_key]
            logger.info(f"Agent stopped and removed: {agent_id}")
            return True

    async def reload_agent(
        self,
        agent_id: str,
        config_path: Optional[Path] = None,
    ) -> bool:
        """Reload a specific agent instance with zero-downtime.

        This method performs a seamless reload by:
        1. Creating and fully starting a new workspace instance (no lock)
        2. Atomically replacing the old instance with the new one (with lock)
        3. Gracefully stopping the old instance (no lock):
           - If active tasks exist: schedule delayed cleanup in background
           - If no active tasks: stop immediately

        The lock is only held during the atomic swap to minimize blocking
        time for other agent operations.

        This ensures that:
        - New requests are immediately handled by the new instance
        - Ongoing SSE/streaming tasks continue uninterrupted
        - Other agents remain accessible during reload
        - The manager returns quickly without waiting for old tasks
        - Old instance is automatically cleaned up after tasks complete

        Args:
            agent_id: Agent ID to reload
            config_path: Config scope; default global ``get_config_path()``.

        Returns:
            bool: True if agent was reloaded, False if not running
        """
        if config_path is None:
            config_path = get_config_path()
        cache_key = _workspace_cache_key(config_path, agent_id)

        # Step 1: Check if agent exists (quick check with lock)
        async with self._lock:
            if cache_key not in self.agents:
                logger.debug(
                    f"Agent not running, will be loaded on next "
                    f"request: {agent_id}",
                )
                return False
            old_instance = self.agents[cache_key]

        logger.info(f"Reloading agent (zero-downtime): {agent_id}")

        # Step 2: Load configuration (outside lock)
        config = load_config(config_path)
        if agent_id not in config.agents.profiles:
            logger.error(
                f"Agent '{agent_id}' not found in configuration "
                f"during reload",
            )
            return False

        agent_ref = config.agents.profiles[agent_id]

        # Step 3: Create and start new workspace instance (outside lock)
        # This is the slow part, but doesn't block other agents
        logger.info(f"Creating new workspace instance: {agent_id}")
        new_instance = Workspace(
            agent_id=agent_id,
            workspace_dir=agent_ref.workspace_dir,
            root_config_path=config_path,
        )

        # Step 3.5: Set reusable components from old instance (if any)
        async with self._lock:
            old_instance = self.agents.get(cache_key)

        if old_instance:
            # Get all reusable services from old instance's ServiceManager
            # pylint: disable=protected-access
            reusable = old_instance._service_manager.get_reusable_services()
            # pylint: enable=protected-access

            if reusable:
                await new_instance.set_reusable_components(reusable)
                logger.info(
                    f"Set reusable components for {agent_id}: "
                    f"{list(reusable.keys())}",
                )

        try:
            await new_instance.start()
            new_instance.set_manager(self)  # Set manager reference
            logger.info(f"New workspace instance started: {agent_id}")
        except Exception as e:
            logger.exception(
                f"Failed to start new workspace instance for {agent_id}: {e}",
            )
            # Try to clean up the failed new instance
            try:
                await new_instance.stop()
            except Exception:
                pass  # Best effort cleanup
            # Old instance is still running and serving requests
            return False

        # Step 4: Atomic swap (minimal lock time)
        # From this point, reload is considered successful
        async with self._lock:
            # Double-check agent still exists
            if cache_key not in self.agents:
                logger.warning(
                    f"Agent {agent_id} was removed during reload, "
                    f"stopping new instance",
                )
                await new_instance.stop()
                return False

            # Swap instances atomically
            old_instance = self.agents[cache_key]
            self.agents[cache_key] = new_instance
            logger.info(f"Workspace instance replaced: {agent_id}")

        # Step 5: Gracefully stop old instance (outside lock)
        # Delegates to helper method to avoid too-many-statements
        await self._graceful_stop_old_instance(old_instance, agent_id)

        return True

    async def cancel_all_cleanup_tasks(self) -> None:
        """Cancel and await all pending delayed cleanup tasks.

        This ensures that any in-progress background cleanups are either
        completed or cleanly cancelled before the manager is torn down.
        Called by stop_all() during shutdown.
        """
        if not self._cleanup_tasks:
            return

        logger.info(
            f"Cancelling {len(self._cleanup_tasks)} pending cleanup "
            f"task(s)...",
        )
        tasks = list(self._cleanup_tasks)
        self._cleanup_tasks.clear()

        for task in tasks:
            if not task.done():
                task.cancel()

        # Await completion of all tasks, collecting exceptions
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("All cleanup tasks cancelled/completed")

    async def stop_all(self):
        """Stop all agent instances.

        Called during application shutdown to clean up resources.
        Cancels any pending delayed cleanup tasks and stops all agents.
        """
        logger.info(f"Stopping all agents ({len(self.agents)} running)...")

        # First, cancel pending cleanup tasks to avoid orphaned instances
        await self.cancel_all_cleanup_tasks()

        cache_keys = list(self.agents.keys())

        for cache_key in cache_keys:
            try:
                instance = self.agents[cache_key]
                await instance.stop()
                logger.debug("Agent stopped: %s", cache_key[1])
            except Exception as e:
                logger.error("Error stopping agent %s: %s", cache_key[1], e)

        self.agents.clear()
        logger.info("All agents stopped")

    def list_loaded_agents(self) -> list[str]:
        """List currently loaded agent IDs with scope (for debugging).

        Returns:
            list[str]: ``agent_id@<config_path>`` per loaded workspace
        """
        return [f"{aid}@{scope}" for scope, aid in self.agents.keys()]

    def is_agent_loaded(
        self,
        agent_id: str,
        config_path: Optional[Path] = None,
    ) -> bool:
        """Check if agent is currently loaded.

        Args:
            agent_id: Agent ID to check
            config_path: Config scope; default global ``get_config_path()``.

        Returns:
            bool: True if agent is loaded and running
        """
        if config_path is None:
            config_path = get_config_path()
        cache_key = _workspace_cache_key(config_path, agent_id)
        return cache_key in self.agents

    async def preload_agent(
        self,
        agent_id: str,
        config_path: Optional[Path] = None,
    ) -> bool:
        """Preload an agent instance during startup.

        Args:
            agent_id: Agent ID to preload
            config_path: Config scope; default global ``get_config_path()``.

        Returns:
            bool: True if successfully preloaded, False if failed
        """
        try:
            await self.get_agent(agent_id, config_path=config_path)
            logger.info(f"Successfully preloaded agent: {agent_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to preload agent {agent_id}: {e}")
            return False

    async def start_all_configured_agents(self) -> dict[str, bool]:
        """Start all enabled agents defined in configuration concurrently.

        Only agents with enabled=True will be started.
        Disabled agents are skipped to save resources.

        When storage isolation is enabled (LAZY_PLATFORM_KEY), there is no
        single global tenant to preload; returns empty and relies on lazy load.

        Returns:
            dict[str, bool]: Mapping of agent_id to success status
        """
        if copaw_storage_isolation_enabled():
            logger.info(
                "Storage isolation enabled: skipping start_all_configured_agents "
                "(per-user lazy load)",
            )
            return {}

        config = load_config()
        # Filter only enabled agents
        enabled_agents = {
            agent_id: ref
            for agent_id, ref in config.agents.profiles.items()
            if getattr(ref, "enabled", True)
        }
        agent_ids = list(enabled_agents.keys())

        if not agent_ids:
            logger.warning("No enabled agents configured in config")
            return {}

        total_agents = len(config.agents.profiles)
        disabled_count = total_agents - len(agent_ids)
        logger.info(
            f"Starting {len(agent_ids)} enabled agent(s) "
            f"({disabled_count} disabled)",
        )

        global_path = get_config_path()

        async def start_single_agent(agent_id: str) -> tuple[str, bool]:
            """Start a single agent with error handling."""
            try:
                logger.info(f"Starting agent: {agent_id}")
                await self.preload_agent(agent_id, config_path=global_path)
                logger.info(f"Agent started successfully: {agent_id}")
                return (agent_id, True)
            except Exception as e:
                logger.error(
                    f"Failed to start agent {agent_id}: {e}. "
                    f"Continuing with other agents...",
                )
                return (agent_id, False)

        # Start all agents concurrently
        results = await asyncio.gather(
            *[start_single_agent(agent_id) for agent_id in agent_ids],
            return_exceptions=False,
        )

        # Build result mapping
        result_map = dict(results)
        success_count = sum(1 for success in result_map.values() if success)
        logger.info(
            f"Agent startup complete: {success_count}/{len(agent_ids)} "
            f"agents started successfully, {disabled_count} disabled",
        )

        return result_map

    def __repr__(self) -> str:
        """String representation of manager."""
        loaded = self.list_loaded_agents()
        return f"MultiAgentManager(loaded_agents={loaded})"
