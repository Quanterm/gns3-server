#!/usr/bin/env python
#
# Copyright (C) 2018 GNS3 Technologies Inc.
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

import sys
import logging
import asyncio
import psutil

if sys.platform.startswith("win"):
    import wmi

from .base_gns3_vm import BaseGNS3VM
from .gns3_vm_error import GNS3VMError
log = logging.getLogger(__name__)


class HyperVGNS3VM(BaseGNS3VM):

    _HYPERV_VM_STATE_ENABLED = 2
    _HYPERV_VM_STATE_DISABLED = 3
    _HYPERV_VM_STATE_PAUSED = 9

    _WMI_JOB_STATUS_STARTED = 4096
    _WMI_JOB_STATE_RUNNING = 4
    _WMI_JOB_STATE_COMPLETED = 7

    def __init__(self, controller):

        self._engine = "hyper-v"
        super().__init__(controller)
        self._conn = None
        self._vm = None
        self._management = None

    def _check_requirements(self):
        """
        Checks if the GNS3 VM can run on Hyper-V.
        """

        if not sys.platform.startswith("win") or sys.getwindowsversion().major < 10:
            raise GNS3VMError("Hyper-V nested virtualization is only supported on Windows 10 and Windows Server 2016 or later")

        conn = wmi.WMI()

        if conn.Win32_Processor()[0].Manufacturer != "GenuineIntel":
            raise GNS3VMError("An Intel processor is required by Hyper-V to support nested virtualization")

        if not conn.Win32_ComputerSystem()[0].HypervisorPresent:
            raise GNS3VMError("Hyper-V is not installed")

        if not conn.Win32_Processor()[0].VirtualizationFirmwareEnabled:
            raise GNS3VMError("Nested Virtualization (VT-x) is not enabled on this system")

    def _connect(self):
        """
        Connects to local host using WMI.
        """

        self._check_requirements()

        try:
            self._conn = wmi.WMI(namespace=r"root\virtualization\v2")
        except wmi.x_wmi as e:
            print("Could not connect to WMI {}".format(e))

        if not self._conn.Msvm_VirtualSystemManagementService():
            raise GNS3VMError("The Windows account running GNS3 does not have the required permissions for Hyper-V")

        self._management = self._conn.Msvm_VirtualSystemManagementService()[0]
        self._vm = self._find_vm(self.vmname)

    def _find_vm(self, vm_name):
        """
        Finds a Hyper-V VM.
        """

        vms = self._conn.Msvm_ComputerSystem(ElementName=vm_name)
        nb_vms = len(vms)
        if nb_vms == 0:
            return None
        elif nb_vms > 1:
            raise GNS3VMError("Duplicate VM name found for {}".format(vm_name))
        else:
            return vms[0]

    def _is_running(self):
        """
        Checks if the VM is running.
        """

        if self._vm is not None and self._vm.EnabledState == HyperVGNS3VM._HYPERV_VM_STATE_ENABLED:
            return True
        return False

    def _set_vcpus_ram(self, vcpus, ram):
        """
        Set the number of vCPU cores and amount of RAM for the GNS3 VM.

        :param vcpus: number of vCPU cores
        :param ram: amount of RAM
        """

        available_vcpus = psutil.cpu_count(logical=False)
        if vcpus > available_vcpus:
            raise GNS3VMError("You have allocated too many vCPUs for the GNS3 VM! (max available is {} vCPUs)".format(available_vcpus))

        try:
            vm_settings = self._vm.associators(wmi_result_class='Msvm_VirtualSystemSettingData')[0]
            mem_settings = vm_settings.associators(wmi_result_class='Msvm_MemorySettingData')[0]
            cpu_settings = vm_settings.associators(wmi_result_class='Msvm_ProcessorSettingData')[0]

            mem_settings.VirtualQuantity = ram
            mem_settings.Reservation = ram
            mem_settings.Limit = ram
            self._management.ModifyResourceSettings(ResourceSettings=[mem_settings.GetText_(1)])

            cpu_settings.VirtualQuantity = vcpus
            cpu_settings.Reservation = vcpus
            cpu_settings.Limit = 100000  # use 100% of CPU
            cpu_settings.ExposeVirtualizationExtensions = True  # allow the VM to use nested virtualization
            self._management.ModifyResourceSettings(ResourceSettings=[cpu_settings.GetText_(1)])

            log.info("GNS3 VM vCPU count set to {} and RAM amount set to {}".format(vcpus, ram))
        except Exception as e:
            raise GNS3VMError("Could not set to {} and RAM amount set to {}: {}".format(vcpus, ram, e))

    @asyncio.coroutine
    def list(self):
        """
        List all Hyper-V VMs
        """

        vms = []
        try:
            for vm in self._conn.Msvm_ComputerSystem():
                if vm.Caption == "Virtual Machine":
                    vms.append(vm.ElementName)
        except wmi.x_wmi as e:
            raise GNS3VMError("Could not list Hyper-V VMs: {}".format(e))
        return vms

    def _get_wmi_obj(self, path):
        """
        Gets the WMI object.
        """

        return wmi.WMI(moniker=path.replace('\\', '/'))

    @asyncio.coroutine
    def _set_state(self, state):
        """
        Set the desired state of the VM
        """

        job_path, ret = self._vm.RequestStateChange(state)
        if ret == HyperVGNS3VM._WMI_JOB_STATUS_STARTED:
            job = self._get_wmi_obj(job_path)
            while job.JobState == HyperVGNS3VM._WMI_JOB_STATE_RUNNING:
                yield from asyncio.sleep(0.1)
                job = self._get_wmi_obj(job_path)
            if job.JobState != HyperVGNS3VM._WMI_JOB_STATE_COMPLETED:
                raise GNS3VMError("Error while changing state: {}".format(job.ErrorSummaryDescription))
        elif ret != 0 or ret != 32775:
            raise GNS3VMError("Failed to change state to {}".format(state))

    @asyncio.coroutine
    def start(self):
        """
        Starts the GNS3 VM.
        """

        if self._conn is None:
            self._connect()

        if not self._is_running():

            log.info("Update GNS3 VM settings")
            # set the number of vCPUs and amount of RAM
            self._set_vcpus_ram(self.vcpus, self.ram)

            # start the VM
            try:
                yield from self._set_state(HyperVGNS3VM._HYPERV_VM_STATE_ENABLED)
            except GNS3VMError as e:
                raise GNS3VMError("Failed to start the GNS3 VM: {}".format(e))
            log.info("GNS3 VM has been started")

        #TODO: get the guest IP address
        #self.ip_address = guest_ip_address
        #log.info("GNS3 VM IP address set to {}".format(guest_ip_address))
        self.running = True

    @asyncio.coroutine
    def suspend(self):
        """
        Suspend the GNS3 VM.
        """

        if self._conn is None:
            self._connect()

        try:
            yield from self._set_state(HyperVGNS3VM._HYPERV_VM_STATE_PAUSED)
        except GNS3VMError as e:
            raise GNS3VMError("Failed to suspend the GNS3 VM: {}".format(e))
        log.info("GNS3 VM has been suspended")
        self.running = False

    @asyncio.coroutine
    def stop(self):
        """
        Stops the GNS3 VM.
        """

        if self._conn is None:
            self._connect()

        try:
            yield from self._set_state(HyperVGNS3VM._HYPERV_VM_STATE_DISABLED)
        except GNS3VMError as e:
            raise GNS3VMError("Failed to stop the GNS3 VM: {}".format(e))
        log.info("GNS3 VM has been stopped")
        self.running = False
