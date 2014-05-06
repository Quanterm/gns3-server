# -*- coding: utf-8 -*-
#
# Copyright (C) 2014 GNS3 Technologies Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
VPCS server module.
"""

import os
import sys
import base64
import tempfile
import fcntl
import struct
import socket
import shutil

from gns3server.modules import IModule
from gns3server.config import Config
import gns3server.jsonrpc as jsonrpc
from .vpcs_device import VPCSDevice
from .vpcs_error import VPCSError
from .nios.nio_udp import NIO_UDP
from .nios.nio_tap import NIO_TAP
from ..attic import find_unused_port

from .schemas import VPCS_CREATE_SCHEMA
from .schemas import VPCS_DELETE_SCHEMA
from .schemas import VPCS_UPDATE_SCHEMA
from .schemas import VPCS_START_SCHEMA
from .schemas import VPCS_STOP_SCHEMA
from .schemas import VPCS_RELOAD_SCHEMA
from .schemas import VPCS_ALLOCATE_UDP_PORT_SCHEMA
from .schemas import VPCS_ADD_NIO_SCHEMA
from .schemas import VPCS_DELETE_NIO_SCHEMA

import logging
log = logging.getLogger(__name__)


class VPCS(IModule):
    """
    VPCS module.

    :param name: module name
    :param args: arguments for the module
    :param kwargs: named arguments for the module
    """

    def __init__(self, name, *args, **kwargs):

        # get the VPCS location
        config = Config.instance()
        VPCS_config = config.get_section_config(name.upper())
        self._VPCS = VPCS_config.get("VPCS")
        if not self._VPCS or not os.path.isfile(self._VPCS):
            VPCS_in_cwd = os.path.join(os.getcwd(), "VPCS")
            if os.path.isfile(VPCS_in_cwd):
                self._VPCS = VPCS_in_cwd
            else:
                # look for VPCS if none is defined or accessible
                for path in os.environ["PATH"].split(":"):
                    try:
                        if "VPCS" in os.listdir(path) and os.access(os.path.join(path, "VPCS"), os.X_OK):
                            self._VPCS = os.path.join(path, "VPCS")
                            break
                    except OSError:
                        continue

        if not self._VPCS:
            log.warning("VPCS binary couldn't be found!")
        elif not os.access(self._VPCS, os.X_OK):
            log.warning("VPCS is not executable")

        # a new process start when calling IModule
        IModule.__init__(self, name, *args, **kwargs)
        self._VPCS_instances = {}
        self._console_start_port_range = 4001
        self._console_end_port_range = 4512
        self._allocated_console_ports = []
        self._current_console_port = self._console_start_port_range
        self._udp_start_port_range = 30001
        self._udp_end_port_range = 40001
        self._current_udp_port = self._udp_start_port_range
        self._host = kwargs["host"]
        self._projects_dir = kwargs["projects_dir"]
        self._tempdir = kwargs["temp_dir"]
        self._working_dir = self._projects_dir
        self._VPCSrc = ""

        # check every 5 seconds
        self._VPCS_callback = self.add_periodic_callback(self._check_VPCS_is_alive, 5000)
        self._VPCS_callback.start()

    def stop(self, signum=None):
        """
        Properly stops the module.

        :param signum: signal number (if called by the signal handler)
        """

        self._VPCS_callback.stop()
        # delete all VPCS instances
        for VPCS_id in self._VPCS_instances:
            VPCS_instance = self._VPCS_instances[VPCS_id]
            VPCS_instance.delete()

        IModule.stop(self, signum)  # this will stop the I/O loop

    def _check_VPCS_is_alive(self):
        """
        Periodic callback to check if VPCS and VPCS are alive
        for each VPCS instance.

        Sends a notification to the client if not.
        """

        for VPCS_id in self._VPCS_instances:
            VPCS_instance = self._VPCS_instances[VPCS_id]
            if VPCS_instance.started and (not VPCS_instance.is_running() or not VPCS_instance.is_VPCS_running()):
                notification = {"module": self.name,
                                "id": VPCS_id,
                                "name": VPCS_instance.name}
                if not VPCS_instance.is_running():
                    stdout = VPCS_instance.read_VPCS_stdout()
                    notification["message"] = "VPCS has stopped running"
                    notification["details"] = stdout
                    self.send_notification("{}.VPCS_stopped".format(self.name), notification)
                elif not VPCS_instance.is_VPCS_running():
                    stdout = VPCS_instance.read_VPCS_stdout()
                    notification["message"] = "VPCS has stopped running"
                    notification["details"] = stdout
                    self.send_notification("{}.VPCS_stopped".format(self.name), notification)
                VPCS_instance.stop()

    def get_VPCS_instance(self, VPCS_id):
        """
        Returns an VPCS device instance.

        :param VPCS_id: VPCS device identifier

        :returns: VPCSDevice instance
        """

        if VPCS_id not in self._VPCS_instances:
            log.debug("VPCS device ID {} doesn't exist".format(VPCS_id), exc_info=1)
            self.send_custom_error("VPCS device ID {} doesn't exist".format(VPCS_id))
            return None
        return self._VPCS_instances[VPCS_id]

    @IModule.route("VPCS.reset")
    def reset(self, request):
        """
        Resets the module.

        :param request: JSON request
        """

        # delete all VPCS instances
        for VPCS_id in self._VPCS_instances:
            VPCS_instance = self._VPCS_instances[VPCS_id]
            VPCS_instance.delete()

        # resets the instance IDs
        VPCSDevice.reset()

        self._VPCS_instances.clear()
        self._remote_server = False
        self._current_console_port = self._console_start_port_range
        self._current_udp_port = self._udp_start_port_range

        log.info("VPCS module has been reset")

    @IModule.route("VPCS.settings")
    def settings(self, request):
        """
        Set or update settings.

        Optional request parameters:
        - working_dir (path to a working directory)
        - project_name
        - console_start_port_range
        - console_end_port_range
        - udp_start_port_range
        - udp_end_port_range

        :param request: JSON request
        """

        if request == None:
            self.send_param_error()
            return

        if "VPCS" in request and request["VPCS"]:
            self._VPCS = request["VPCS"]
            log.info("VPCS path set to {}".format(self._VPCS))

        if "working_dir" in request:
            new_working_dir = request["working_dir"]
            log.info("this server is local with working directory path to {}".format(new_working_dir))
        else:
            new_working_dir = os.path.join(self._projects_dir, request["project_name"] + ".gns3")
            log.info("this server is remote with working directory path to {}".format(new_working_dir))
            if self._projects_dir != self._working_dir != new_working_dir:
                if not os.path.isdir(new_working_dir):
                    try:
                        shutil.move(self._working_dir, new_working_dir)
                    except OSError as e:
                        log.error("could not move working directory from {} to {}: {}".format(self._working_dir,
                                                                                              new_working_dir,
                                                                                              e))
                        return

        # update the working directory if it has changed
        if self._working_dir != new_working_dir:
            self._working_dir = new_working_dir
            for VPCS_id in self._VPCS_instances:
                VPCS_instance = self._VPCS_instances[VPCS_id]
                VPCS_instance.working_dir = self._working_dir

        if "console_start_port_range" in request and "console_end_port_range" in request:
            self._console_start_port_range = request["console_start_port_range"]
            self._console_end_port_range = request["console_end_port_range"]

        if "udp_start_port_range" in request and "udp_end_port_range" in request:
            self._udp_start_port_range = request["udp_start_port_range"]
            self._udp_end_port_range = request["udp_end_port_range"]

        log.debug("received request {}".format(request))

    def test_result(self, message, result="error"):
        """
        """

        return {"result": result, "message": message}

    @IModule.route("VPCS.test_settings")
    def test_settings(self, request):
        """
        """

        response = []

        self.send_response(response)

    @IModule.route("VPCS.create")
    def VPCS_create(self, request):
        """
        Creates a new VPCS instance.

        Mandatory request parameters:
        - path (path to the VPCS executable)

        Optional request parameters:
        - name (VPCS name)

        Response parameters:
        - id (VPCS instance identifier)
        - name (VPCS name)
        - default settings

        :param request: JSON request
        """

        # validate the request
        if not self.validate_request(request, VPCS_CREATE_SCHEMA):
            return

        name = None
        if "name" in request:
            name = request["name"]
        VPCS_path = request["path"]

        try:
            try:
                os.makedirs(self._working_dir)
            except FileExistsError:
                pass
            except OSError as e:
                raise VPCSError("Could not create working directory {}".format(e))

            VPCS_instance = VPCSDevice(VPCS_path, self._working_dir, host=self._host, name=name)
            # find a console port
            if self._current_console_port > self._console_end_port_range:
                self._current_console_port = self._console_start_port_range
            try:
                VPCS_instance.console = find_unused_port(self._current_console_port, self._console_end_port_range, self._host)
            except Exception as e:
                raise VPCSError(e)
            self._current_console_port += 1
        except VPCSError as e:
            self.send_custom_error(str(e))
            return

        response = {"name": VPCS_instance.name,
                    "id": VPCS_instance.id}

        defaults = VPCS_instance.defaults()
        response.update(defaults)
        self._VPCS_instances[VPCS_instance.id] = VPCS_instance
        self.send_response(response)

    @IModule.route("VPCS.delete")
    def VPCS_delete(self, request):
        """
        Deletes an VPCS instance.

        Mandatory request parameters:
        - id (VPCS instance identifier)

        Response parameter:
        - True on success

        :param request: JSON request
        """

        # validate the request
        if not self.validate_request(request, VPCS_DELETE_SCHEMA):
            return

        # get the instance
        VPCS_instance = self.get_VPCS_instance(request["id"])
        if not VPCS_instance:
            return

        try:
            VPCS_instance.delete()
            del self._VPCS_instances[request["id"]]
        except VPCSError as e:
            self.send_custom_error(str(e))
            return

        self.send_response(True)

    @IModule.route("VPCS.update")
    def VPCS_update(self, request):
        """
        Updates an VPCS instance

        Mandatory request parameters:
        - id (VPCS instance identifier)

        Optional request parameters:
        - any setting to update
        - startup_config_base64 (startup-config base64 encoded)

        Response parameters:
        - updated settings

        :param request: JSON request
        """

        # validate the request
        if not self.validate_request(request, VPCS_UPDATE_SCHEMA):
            return

        # get the instance
        VPCS_instance = self.get_VPCS_instance(request["id"])
        if not VPCS_instance:
            return

        response = {}
        try:
            # a new startup-config has been pushed
            if "startup_config_base64" in request:
                config = base64.decodestring(request["startup_config_base64"].encode("utf-8")).decode("utf-8")
                config = "!\n" + config.replace("\r", "")
                config = config.replace('%h', VPCS_instance.name)
                config_path = os.path.join(VPCS_instance.working_dir, "startup-config")
                try:
                    with open(config_path, "w") as f:
                        log.info("saving startup-config to {}".format(config_path))
                        f.write(config)
                except OSError as e:
                    raise VPCSError("Could not save the configuration {}: {}".format(config_path, e))
                # update the request with the new local startup-config path
                request["startup_config"] = os.path.basename(config_path)

        except VPCSError as e:
            self.send_custom_error(str(e))
            return

        # update the VPCS settings
        for name, value in request.items():
            if hasattr(VPCS_instance, name) and getattr(VPCS_instance, name) != value:
                try:
                    setattr(VPCS_instance, name, value)
                    response[name] = value
                except VPCSError as e:
                    self.send_custom_error(str(e))
                    return

        self.send_response(response)

    @IModule.route("VPCS.start")
    def vm_start(self, request):
        """
        Starts an VPCS instance.

        Mandatory request parameters:
        - id (VPCS instance identifier)

        Response parameters:
        - True on success

        :param request: JSON request
        """

        # validate the request
        if not self.validate_request(request, VPCS_START_SCHEMA):
            return

        # get the instance
        VPCS_instance = self.get_VPCS_instance(request["id"])
        if not VPCS_instance:
            return

        try:
            log.debug("starting VPCS with command: {}".format(VPCS_instance.command()))
            VPCS_instance.VPCS = self._VPCS
            VPCS_instance.VPCSrc = self._VPCSrc
            VPCS_instance.start()
        except VPCSError as e:
            self.send_custom_error(str(e))
            return
        self.send_response(True)

    @IModule.route("VPCS.stop")
    def vm_stop(self, request):
        """
        Stops an VPCS instance.

        Mandatory request parameters:
        - id (VPCS instance identifier)

        Response parameters:
        - True on success

        :param request: JSON request
        """

        # validate the request
        if not self.validate_request(request, VPCS_STOP_SCHEMA):
            return

        # get the instance
        VPCS_instance = self.get_VPCS_instance(request["id"])
        if not VPCS_instance:
            return

        try:
            VPCS_instance.stop()
        except VPCSError as e:
            self.send_custom_error(str(e))
            return
        self.send_response(True)

    @IModule.route("VPCS.reload")
    def vm_reload(self, request):
        """
        Reloads an VPCS instance.

        Mandatory request parameters:
        - id (VPCS identifier)

        Response parameters:
        - True on success

        :param request: JSON request
        """

        # validate the request
        if not self.validate_request(request, VPCS_RELOAD_SCHEMA):
            return

        # get the instance
        VPCS_instance = self.get_VPCS_instance(request["id"])
        if not VPCS_instance:
            return

        try:
            if VPCS_instance.is_running():
                VPCS_instance.stop()
            VPCS_instance.start()
        except VPCSError as e:
            self.send_custom_error(str(e))
            return
        self.send_response(True)

    @IModule.route("VPCS.allocate_udp_port")
    def allocate_udp_port(self, request):
        """
        Allocates a UDP port in order to create an UDP NIO.

        Mandatory request parameters:
        - id (VPCS identifier)
        - port_id (unique port identifier)

        Response parameters:
        - port_id (unique port identifier)
        - lport (allocated local port)

        :param request: JSON request
        """

        # validate the request
        if not self.validate_request(request, VPCS_ALLOCATE_UDP_PORT_SCHEMA):
            return

        # get the instance
        VPCS_instance = self.get_VPCS_instance(request["id"])
        if not VPCS_instance:
            return

        try:

            # find a UDP port
            if self._current_udp_port >= self._udp_end_port_range:
                self._current_udp_port = self._udp_start_port_range
            try:
                port = find_unused_port(self._current_udp_port, self._udp_end_port_range, host=self._host, socket_type="UDP")
            except Exception as e:
                raise VPCSError(e)
            self._current_udp_port += 1

            log.info("{} [id={}] has allocated UDP port {} with host {}".format(VPCS_instance.name,
                                                                                VPCS_instance.id,
                                                                                port,
                                                                                self._host))
            response = {"lport": port}

        except VPCSError as e:
            self.send_custom_error(str(e))
            return

        response["port_id"] = request["port_id"]
        self.send_response(response)

    def _check_for_privileged_access(self, device):
        """
        Check if VPCS can access Ethernet and TAP devices.

        :param device: device name
        """

        # we are root, so VPCS should have privileged access too
        if os.geteuid() == 0:
            return

        # test if VPCS has the CAP_NET_RAW capability
        if "security.capability" in os.listxattr(self._VPCS):
            try:
                caps = os.getxattr(self._VPCS, "security.capability")
                # test the 2nd byte and check if the 13th bit (CAP_NET_RAW) is set
                if struct.unpack("<IIIII", caps)[1] & 1 << 13:
                    return
            except Exception as e:
                log.error("could not determine if CAP_NET_RAW capability is set for {}: {}".format(self._VPCS, e))
                return

        raise VPCSError("{} has no privileged access to {}.".format(self._VPCS, device))

    @IModule.route("VPCS.add_nio")
    def add_nio(self, request):
        """
        Adds an NIO (Network Input/Output) for an VPCS instance.

        Mandatory request parameters:
        - id (VPCS instance identifier)
        - slot (slot number)
        - port (port number)
        - port_id (unique port identifier)
        - nio (one of the following)
            - type "nio_udp"
                - lport (local port)
                - rhost (remote host)
                - rport (remote port)
            - type "nio_tap"
                - tap_device (TAP device name e.g. tap0)

        Response parameters:
        - port_id (unique port identifier)

        :param request: JSON request
        """

        # validate the request
        if not self.validate_request(request, VPCS_ADD_NIO_SCHEMA):
            return

        # get the instance
        VPCS_instance = self.get_VPCS_instance(request["id"])
        if not VPCS_instance:
            return

        slot = request["slot"]
        port = request["port"]
        try:
            nio = None
            if request["nio"]["type"] == "nio_udp":
                lport = request["nio"]["lport"]
                rhost = request["nio"]["rhost"]
                rport = request["nio"]["rport"]
                nio = NIO_UDP(lport, rhost, rport)
            elif request["nio"]["type"] == "nio_tap":
                tap_device = request["nio"]["tap_device"]
                self._check_for_privileged_access(tap_device)
                nio = NIO_TAP(tap_device)
            if not nio:
                raise VPCSError("Requested NIO does not exist or is not supported: {}".format(request["nio"]["type"]))
        except VPCSError as e:
            self.send_custom_error(str(e))
            return

        try:
            VPCS_instance.slot_add_nio_binding(slot, port, nio)
        except VPCSError as e:
            self.send_custom_error(str(e))
            return

        self.send_response({"port_id": request["port_id"]})

    @IModule.route("VPCS.delete_nio")
    def delete_nio(self, request):
        """
        Deletes an NIO (Network Input/Output).

        Mandatory request parameters:
        - id (VPCS instance identifier)
        - slot (slot identifier)
        - port (port identifier)

        Response parameters:
        - True on success

        :param request: JSON request
        """

        # validate the request
        if not self.validate_request(request, VPCS_DELETE_NIO_SCHEMA):
            return

        # get the instance
        VPCS_instance = self.get_VPCS_instance(request["id"])
        if not VPCS_instance:
            return

        slot = request["slot"]
        port = request["port"]
        try:
            VPCS_instance.slot_remove_nio_binding(slot, port)
        except VPCSError as e:
            self.send_custom_error(str(e))
            return

        self.send_response(True)

    @IModule.route("VPCS.echo")
    def echo(self, request):
        """
        Echo end point for testing purposes.

        :param request: JSON request
        """

        if request == None:
            self.send_param_error()
        else:
            log.debug("received request {}".format(request))
            self.send_response(request)
