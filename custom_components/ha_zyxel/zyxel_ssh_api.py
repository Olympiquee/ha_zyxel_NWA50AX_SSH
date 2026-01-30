"""
API client for Zyxel NWA50AX via SSH - Optimized for V7.10(ABYW.3).
This implementation supports asyncssh (preferred) and paramiko (fallback).
It provides a persistent connection for both libraries.
"""
import logging
import asyncio
import re
from typing import Any, Optional
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

# Ensure flags are always defined
HAS_ASYNCSSH = False
HAS_PARAMIKO = False

try:
    import asyncssh  # type: ignore
    HAS_ASYNCSSH = True
except Exception:
    HAS_ASYNCSSH = False

try:
    import paramiko  # type: ignore
    HAS_PARAMIKO = True
except Exception:
    HAS_PARAMIKO = False


class ZyxelSSHAPI:
    """Class to communicate with Zyxel NWA50AX via SSH."""

    def __init__(self, host: str, username: str, password: str, port: int = 22) -> None:
        """Initialize the API."""
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self._conn = None

        if not HAS_ASYNCSSH and not HAS_PARAMIKO:
            raise ImportError(
                "Neither asyncssh nor paramiko is installed. Install one: pip install asyncssh or pip install paramiko"
            )

    async def async_connect(self) -> bool:
        """Connect to the device via SSH."""
        try:
            if HAS_ASYNCSSH:
                # Use asyncssh (preferred - async)
                self._conn = await asyncio.wait_for(
                    asyncssh.connect(
                        self.host,
                        port=self.port,
                        username=self.username,
                        password=self.password,
                        known_hosts=None,
                    ),
                    timeout=10.0,
                )
                _LOGGER.info("Successfully connected via SSH (asyncssh)")

            elif HAS_PARAMIKO:
                # Use paramiko in a thread executor (sync library)
                loop = asyncio.get_event_loop()
                self._conn = await loop.run_in_executor(None, self._connect_paramiko_sync)
                _LOGGER.info("Successfully connected via SSH (paramiko)")

            else:
                raise ImportError("No SSH library available")

            return True

        except Exception as err:
            _LOGGER.error("SSH connection failed: %s", err)
            return False

    async def async_disconnect(self) -> None:
        """Disconnect from the device."""
        try:
            if self._conn and HAS_ASYNCSSH:
                self._conn.close()
                await self._conn.wait_closed()
                self._conn = None

            elif self._conn and HAS_PARAMIKO:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._disconnect_paramiko_sync)
                self._conn = None

        except Exception as err:
            _LOGGER.debug("Error while disconnecting SSH: %s", err)

    async def async_execute_command(self, command: str) -> Optional[str]:
        """Execute a command on the device."""
        try:
            if HAS_ASYNCSSH and self._conn:
                result = await self._conn.run(command, check=True, timeout=15)
                return result.stdout

            elif HAS_PARAMIKO:
                # Run blocking command in executor using paramiko client
                return await asyncio.get_event_loop().run_in_executor(None, self._execute_command_sync, command)

            else:
                _LOGGER.error("No SSH library available")
                return None

        except Exception as err:
            _LOGGER.error("Error executing command '%s': %s", command, err)
            return None

    def _connect_paramiko_sync(self):
        """Create and return a connected paramiko.SSHClient (sync)."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=10,
        )
        return ssh

    def _disconnect_paramiko_sync(self):
        """Close a paramiko SSHClient connection (sync)."""
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass

    def _execute_command_sync(self, command: str) -> Optional[str]:
        """Execute command synchronously with paramiko."""
        # Prefer using the persistent client if available
        try:
            if HAS_PARAMIKO and self._conn:
                stdin, stdout, stderr = self._conn.exec_command(command, timeout=15)
                output = stdout.read().decode("utf-8", errors="ignore")
                return output

            # Fallback: create a temporary client for single command
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            try:
                ssh.connect(
                    self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    timeout=10,
                )

                stdin, stdout, stderr = ssh.exec_command(command, timeout=15)
                output = stdout.read().decode("utf-8", errors="ignore")

                ssh.close()
                return output

            except Exception as err:
                _LOGGER.error("Paramiko command failed: %s", err)
                try:
                    ssh.close()
                except Exception:
                    pass
                return None

        except Exception as err:
            _LOGGER.error("_execute_command_sync error: %s", err)
            return None

    async def async_get_data(self) -> dict[str, Any]:
        """Get all device data using validated commands."""
        data = {
            "device_info": {},
            "status": {},
            "clients": [],
            "network": {},
            "radio": {},
        }

        try:
            # 1. Version et modèle (show version)
            version_output = await self.async_execute_command("show version")
            if version_output:
                data["device_info"] = self._parse_version(version_output)

            # 2. Uptime (show system uptime)
            uptime_output = await self.async_execute_command("show system uptime")
            if uptime_output:
                data["status"]["uptime"] = self._parse_uptime(uptime_output)

            # 3. CPU (show cpu all)
            cpu_output = await self.async_execute_command("show cpu all")
            if cpu_output:
                data["status"]["cpu"] = self._parse_cpu(cpu_output)

            # 4. Mémoire (show mem status)
            mem_output = await self.async_execute_command("show mem status")
            if mem_output:
                data["status"]["memory"] = self._parse_memory(mem_output)

            # 5. Clients WiFi (show wireless-hal station info) - LA PLUS IMPORTANTE
            clients_output = await self.async_execute_command("show wireless-hal station info")
            if clients_output:
                data["clients"] = self._parse_clients(clients_output)

            # 6. Interfaces (show interface all)
            interface_output = await self.async_execute_command("show interface all")
            if interface_output:
                data["network"] = self._parse_interfaces(interface_output)

            # 7. Info WLAN (show wlan all)
            wlan_output = await self.async_execute_command("show wlan all")
            if wlan_output:
                data["radio"] = self._parse_wlan(wlan_output)

            # 8. Port status (show port status)
            port_output = await self.async_execute_command("show port status")
            if port_output:
                data["network"]["port"] = self._parse_port_status(port_output)

        except Exception as err:
            _LOGGER.error("Error fetching device data: %s", err)

        return data

    # --- Parsers unchanged (kept as in original file) ---

    def _parse_version(self, output: str) -> dict[str, Any]:
        info = {"model": "Unknown", "firmware": "Unknown", "build_date": "Unknown"}
        model_match = re.search(r'model\s*:\s*(.+)', output)
        if model_match:
            info["model"] = model_match.group(1).strip()
        firmware_match = re.search(r'firmware version\s*:\s*(.+)', output)
        if firmware_match:
            info["firmware"] = firmware_match.group(1).strip()
        build_match = re.search(r'build date\s*:\s*(.+)', output)
        if build_match:
            info["build_date"] = build_match.group(1).strip()
        return info

    def _parse_uptime(self, output: str) -> int:
        uptime_seconds = 0
        match = re.search(r'(\d+)\s+days?\s+(\d+):(\d+):(\d+)', output)
        if match:
            days = int(match.group(1))
            hours = int(match.group(2))
            minutes = int(match.group(3))
            seconds = int(match.group(4))
            uptime_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
        else:
            match = re.search(r'(\d+):(\d+):(\d+)', output)
            if match:
                hours = int(match.group(1))
                minutes = int(match.group(2))
                seconds = int(match.group(3))
                uptime_seconds = hours * 3600 + minutes * 60 + seconds
        return uptime_seconds

    def _parse_cpu(self, output: str) -> dict[str, Any]:
        cpu_data = {"current": 0, "avg_1min": 0, "avg_5min": 0, "cores": []}
        core_pattern = r'CPU core (\d+) utilization:\s*(\d+)\s*%'
        core_1min_pattern = r'CPU core (\d+) utilization for 1 min:\s*(\d+)\s*%'
        core_5min_pattern = r'CPU core (\d+) utilization for 5 min:\s*(\d+)\s*%'
        cores_current = re.findall(core_pattern, output)
        cores_1min = re.findall(core_1min_pattern, output)
        cores_5min = re.findall(core_5min_pattern, output)
        if cores_current:
            cpu_data["current"] = sum(int(c[1]) for c in cores_current) // len(cores_current)
            cpu_data["cores"] = [int(c[1]) for c in cores_current]
        if cores_1min:
            cpu_data["avg_1min"] = sum(int(c[1]) for c in cores_1min) // len(cores_1min)
        if cores_5min:
            cpu_data["avg_5min"] = sum(int(c[1]) for c in cores_5min) // len(cores_5min)
        return cpu_data

    def _parse_memory(self, output: str) -> int:
        match = re.search(r'memory usage:\s*(\d+)\s*%', output)
        if match:
            return int(match.group(1))
        return 0

    def _parse_clients(self, output: str) -> list[dict[str, Any]]:
        clients = []
        client_blocks = re.split(r'index:\s*\d+', output)
        for block in client_blocks[1:]:
            client = {}
            mac_match = re.search(r'MAC:\s*([\da-fA-F:]+)', block)
            if mac_match:
                client["mac"] = mac_match.group(1).upper()
            ip_match = re.search(r'IPv4:\s*([\d.]+)', block)
            if ip_match:
                client["ip"] = ip_match.group(1)
            ssid_match = re.search(r'Display SSID:\s*(.+)', block)
            if ssid_match:
                client["ssid"] = ssid_match.group(1).strip()
            elif re.search(r'SSID:\s*(.+)', block):
                client["ssid"] = re.search(r'SSID:\s*(.+)', block).group(1).strip()
            security_match = re.search(r'Security:\s*(.+)', block)
            if security_match:
                client["security"] = security_match.group(1).strip()
            rssi_dbm_match = re.search(r'RSSI dBm:\s*(-?\d+)', block)
            if rssi_dbm_match:
                client["rssi_dbm"] = int(rssi_dbm_match.group(1))
            rssi_match = re.search(r'RSSI:\s*(\d+)', block)
            if rssi_match:
                client["rssi_percent"] = int(rssi_match.group(1))
            band_match = re.search(r'Band:\s*([\dG.Hz]+)', block)
            if band_match:
                client["band"] = band_match.group(1)
            slot_match = re.search(r'Slot:\s*(\d+)', block)
            if slot_match:
                client["slot"] = int(slot_match.group(1))
            tx_match = re.search(r'TxRate:\s*(\d+)M', block)
            if tx_match:
                client["tx_rate"] = int(tx_match.group(1))
            rx_match = re.search(r'RxRate:\s*(\d+)M', block)
            if rx_match:
                client["rx_rate"] = int(rx_match.group(1))
            capability_match = re.search(r'Capability:\s*(.+)', block)
            if capability_match:
                client["capability"] = capability_match.group(1).strip()
            time_match = re.search(r'Time:\s*(.+)', block)
            if time_match:
                client["connected_since"] = time_match.group(1).strip()
            if client.get("mac"):
                clients.append(client)
        return clients

    def _parse_interfaces(self, output: str) -> dict[str, Any]:
        network = {"ip_address": "Unknown", "netmask": "Unknown", "interfaces": []}
        lan_match = re.search(r'lan\s+Up\s+([\d.]+)\s+([\d.]+)', output)
        if lan_match:
            network["ip_address"] = lan_match.group(1)
            network["netmask"] = lan_match.group(2)
        interface_lines = re.findall(r'(\d+)\s+(\S+)\s+(Up|Down|n/a)\s+([\d.]+|n/a)', output)
        for iface in interface_lines:
            network["interfaces"].append({"name": iface[1], "status": iface[2], "ip": iface[3] if iface[3] != "n/a" else None})
        return network

    def _parse_wlan(self, output: str) -> dict[str, Any]:
        radio = {"slot1_active": False, "slot1_band": "Unknown", "slot1_ssids": [], "slot2_active": False, "slot2_band": "Unknown", "slot2_ssids": []}
        slot1_match = re.search(r'slot: slot1.*?Activate: (\w+).*?Band: ([\dG.]+)', output, re.DOTALL)
        if slot1_match:
            radio["slot1_active"] = slot1_match.group(1).lower() == "yes"
            radio["slot1_band"] = slot1_match.group(2)
        slot1_block = re.search(r'slot: slot1(.*?)(?:slot: slot2|$)', output, re.DOTALL)
        if slot1_block:
            ssids = re.findall(r'SSID_profile_\d+:\s*(\S+)', slot1_block.group(1))
            radio["slot1_ssids"] = [s for s in ssids if s]
        slot2_match = re.search(r'slot: slot2.*?Activate: (\w+).*?Band: ([\dG.]+)', output, re.DOTALL)
        if slot2_match:
            radio["slot2_active"] = slot2_match.group(1).lower() == "yes"
            radio["slot2_band"] = slot2_match.group(2)
        slot2_block = re.search(r'slot: slot2(.*?)$', output, re.DOTALL)
        if slot2_block:
            ssids = re.findall(r'SSID_profile_\d+:\s*(\S+)', slot2_block.group(1))
            radio["slot2_ssids"] = [s for s in ssids if s]
        return radio

    def _parse_port_status(self, output: str) -> dict[str, Any]:
        port = {"status": "Unknown", "speed": "Unknown", "tx_bytes": 0, "rx_bytes": 0, "tx_rate": 0, "rx_rate": 0, "uptime": "Unknown"}
        port_match = re.search(r'1\s+(\S+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)\s+(\d+)\s+([\d:]+)\s+\d+\s+(\d+)\s+(\d+)', output)
        if port_match:
            port["status"] = port_match.group(1)
            port["tx_rate"] = int(port_match.group(2))
            port["rx_rate"] = int(port_match.group(3))
            port["uptime"] = port_match.group(4)
            port["tx_bytes"] = int(port_match.group(5))
            port["rx_bytes"] = int(port_match.group(6))
            if "/" in port["status"]:
                port["speed"] = port["status"].split("/")[0]
        return port

    async def async_reboot(self) -> bool:
        try:
            result = await self.async_execute_command("reboot")
            if result is not None:
                _LOGGER.info("Reboot command sent")
                return True
            return False
        except Exception as err:
            _LOGGER.error("Error rebooting device: %s", err)
            return False

    async def async_toggle_guest_ssid(self, enable: bool) -> bool:
        try:
            if enable:
                commands = [
                    "configure terminal",
                    "wlan-ssid-profile Guest",
                    "no ssid-schedule",
                    "exit",
                    "write",
                ]
            else:
                commands = [
                    "configure terminal",
                    "wlan-ssid-profile Guest",
                    "ssid-schedule",
                    "exit",
                    "write",
                ]
            for cmd in commands:
                await self.async_execute_command(cmd)
                await asyncio.sleep(0.5)
            _LOGGER.info("Guest SSID schedule %s", "disabled (always on)" if enable else "enabled")
            return True
        except Exception as err:
            _LOGGER.error("Error toggling guest SSID: %s", err)
            return False