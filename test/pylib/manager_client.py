#
# Copyright (C) 2022-present ScyllaDB
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Manager client.
   Communicates with Manager server via socket.
   Provides helper methods to test cases.
   Manages driver refresh when cluster is cycled.
"""

from typing import List, Optional, Callable, NamedTuple
import logging
from test.pylib.rest_client import UnixRESTClient, ScyllaRESTAPIClient
from test.pylib.util import wait_for
from test.pylib.internal_types import ServerNum, IPAddress, HostID, ServerInfo
from cassandra.cluster import Session as CassandraSession  # type: ignore # pylint: disable=no-name-in-module
from cassandra.cluster import Cluster as CassandraCluster  # type: ignore # pylint: disable=no-name-in-module
import aiohttp


logger = logging.getLogger(__name__)


class ManagerClient():
    """Helper Manager API client
    Args:
        sock_path (str): path to an AF_UNIX socket where Manager server is listening
        con_gen (Callable): generator function for CQL driver connection to a cluster
    """
    # pylint: disable=too-many-public-methods

    def __init__(self, sock_path: str, port: int, use_ssl: bool,
                 con_gen: Optional[Callable[[List[IPAddress], int, bool], CassandraSession]]) \
                         -> None:
        self.port = port
        self.use_ssl = use_ssl
        self.con_gen = con_gen
        self.ccluster: Optional[CassandraCluster] = None
        self.cql: Optional[CassandraSession] = None
        # A client for communicating with ScyllaClusterManager (server)
        self.client = UnixRESTClient(sock_path)
        self.api = ScyllaRESTAPIClient()

    async def stop(self):
        """Close driver"""
        self.driver_close()

    async def driver_connect(self) -> None:
        """Connect to cluster"""
        if self.con_gen is not None:
            servers = [s_info.ip_addr for s_info in await self.running_servers()]
            logger.debug("driver connecting to %s", servers)
            self.ccluster = self.con_gen(servers, self.port, self.use_ssl)
            self.cql = self.ccluster.connect()

    def driver_close(self) -> None:
        """Disconnect from cluster"""
        if self.ccluster is not None:
            logger.debug("shutting down driver")
            self.ccluster.shutdown()
            self.ccluster = None
        self.cql = None

    # Make driver update endpoints from remote connection
    def _driver_update(self) -> None:
        if self.ccluster is not None:
            logger.debug("refresh driver node list")
            self.ccluster.control_connection.refresh_node_list_and_token_map()

    async def before_test(self, test_case_name: str) -> None:
        """Before a test starts check if cluster needs cycling and update driver connection"""
        logger.debug("before_test for %s", test_case_name)
        dirty = await self.is_dirty()
        if dirty:
            self.driver_close()  # Close driver connection to old cluster
        try:
            cluster_str = await self.client.get_text(f"/cluster/before-test/{test_case_name}")
            logger.info(f"Using cluster: {cluster_str} for test {test_case_name}")
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"Failed before test check {exc}") from exc
        if self.cql is None:
            # TODO: if cluster is not up yet due to taking long and HTTP timeout, wait for it
            # await self._wait_for_cluster()
            await self.driver_connect()  # Connect driver to new cluster

    async def after_test(self, test_case_name: str) -> None:
        """Tell harness this test finished"""
        logger.debug("after_test for %s", test_case_name)
        await self.client.get(f"/cluster/after-test")

    async def is_manager_up(self) -> bool:
        """Check if Manager server is up"""
        ret = await self.client.get_text("/up")
        return ret == "True"

    async def is_cluster_up(self) -> bool:
        """Check if cluster is up"""
        ret = await self.client.get_text("/cluster/up")
        return ret == "True"

    async def is_dirty(self) -> bool:
        """Check if current cluster dirty."""
        dirty = await self.client.get_text("/cluster/is-dirty")
        return dirty == "True"

    async def replicas(self) -> int:
        """Get number of configured replicas for the cluster (replication factor)"""
        resp = await self.client.get_text("/cluster/replicas")
        return int(resp)

    async def running_servers(self) -> List[ServerInfo]:
        """Get List of server info (id and IP address) of running servers"""
        try:
            server_info_list = await self.client.get_json("/cluster/running-servers")
        except RuntimeError as exc:
            raise Exception("Failed to get list of running servers") from exc
        assert isinstance(server_info_list, list), "running_servers got unknown data type"
        return [ServerInfo(ServerNum(int(info[0])), IPAddress(info[1]))
                for info in server_info_list]

    async def mark_dirty(self) -> None:
        """Manually mark current cluster dirty.
           To be used when a server was modified outside of this API."""
        await self.client.get_text("/cluster/mark-dirty")

    async def server_stop(self, server_id: ServerNum) -> None:
        """Stop specified server"""
        logger.debug("ManagerClient stopping %s", server_id)
        await self.client.get_text(f"/cluster/server/{server_id}/stop")

    async def server_stop_gracefully(self, server_id: ServerNum) -> None:
        """Stop specified server gracefully"""
        logger.debug("ManagerClient stopping gracefully %s", server_id)
        await self.client.get_text(f"/cluster/server/{server_id}/stop_gracefully")

    async def server_start(self, server_id: ServerNum) -> None:
        """Start specified server"""
        logger.debug("ManagerClient starting %s", server_id)
        await self.client.get_text(f"/cluster/server/{server_id}/start")
        self._driver_update()

    async def server_restart(self, server_id: ServerNum) -> None:
        """Restart specified server"""
        logger.debug("ManagerClient restarting %s", server_id)
        await self.client.get_text(f"/cluster/server/{server_id}/restart")
        self._driver_update()

    async def server_add(self) -> ServerInfo:
        """Add a new server"""
        try:
            server_info = await self.client.get_json("/cluster/addserver")
        except Exception as exc:
            raise Exception("Failed to add server") from exc
        try:
            s_info = ServerInfo(ServerNum(int(server_info["server_id"])),
                                IPAddress(server_info["ip_addr"]))
        except Exception as exc:
            raise RuntimeError(f"server_add got invalid server data {server_info}") from exc
        logger.debug("ManagerClient added %s", s_info)
        self._driver_update()
        return s_info

    async def remove_node(self, initiator_id: ServerNum, server_id: ServerNum,
                          ignore_dead: List[IPAddress] = []) -> None:
        """Invoke remove node Scylla REST API for a specified server"""
        logger.debug("ManagerClient remove node %s on initiator %s", server_id, initiator_id)
        data = {"server_id": server_id, "ignore_dead": ignore_dead}
        await self.client.put_json(f"/cluster/remove-node/{initiator_id}", data)
        self._driver_update()

    async def decommission_node(self, server_id: ServerNum) -> None:
        """Tell a node to decommission with Scylla REST API"""
        logger.debug("ManagerClient decommission %s", server_id)
        await self.client.get_text(f"/cluster/decommission-node/{server_id}")
        self._driver_update()

    async def server_get_config(self, server_id: ServerNum) -> dict[str, object]:
        data = await self.client.get_json(f"/cluster/server/{server_id}/get_config")
        assert isinstance(data, dict), f"server_get_config: got {type(data)} expected dict"
        return data

    async def server_update_config(self, server_id: ServerNum, key: str, value: object) -> None:
        await self.client.put_json(f"/cluster/server/{server_id}/update_config",
                                   {"key": key, "value": value})

    async def wait_for_host_known(self, dst_server_id: str, expect_host_id: str,
                                  deadline: Optional[float] = None) -> None:
        """Waits until dst_server_id knows about expect_host_id, with timeout"""
        async def host_is_known():
            host_id_map = await self.api.get_host_id_map(dst_server_id)
            return True if any(entry for entry in host_id_map if entry['value'] == expect_host_id) else None

        return await wait_for(host_is_known, deadline or (time() + 30))

    async def wait_for_host_down(self, dst_server_id: str, server_id: str, deadline: Optional[float] = None) -> None:
        """Waits for dst_server_id to consider server_id as down, with timeout"""
        async def host_is_down():
            down_endpoints = await self.api.get_down_endpoints(dst_server_id)
            return True if server_id in down_endpoints else None

        return await wait_for(host_is_down, deadline or (time() + 30))

    async def get_host_ip(self, server_id: ServerNum) -> IPAddress:
        """Get host IP Address"""
        try:
            server_ip = await self.client.get_text(f"/cluster/host-ip/{server_id}")
        except Exception as exc:
            raise Exception(f"Failed to get host IP address for server {server_id}") from exc
        return IPAddress(server_ip)

    async def get_host_id(self, server_id: ServerNum) -> HostID:
        """Get local host id of a server"""
        try:
            host_id = await self.client.get_text(f"/cluster/host-id/{server_id}")
        except Exception as exc:
            raise Exception(f"Failed to get local host id address for server {server_id}") from exc
        return HostID(host_id)
