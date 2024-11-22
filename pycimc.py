#!/usr/bin/env python

__author__ = 'Rob Horner (robert@horners.org)'

import xml.etree.ElementTree as ET
from collections import namedtuple, defaultdict
import time, sys
import inspect
from pprint import pprint
import requests
import logging
from exception_mapper import *

import warnings
warnings.filterwarnings(action='ignore', module='.*OpenSSL.*')
import requests  # noqa: E402
from requests.packages.urllib3.exceptions import \
    InsecureRequestWarning  # noqa: E402
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

LOGIN_TIMEOUT = 5.0
REQUEST_TIMEOUT = 10.0
CREATE_DRIVE_TIMEOUT = 60.0

Version = namedtuple('Version',['major','minor','maintenance'])   # Class variable - data shared
VirtualDrive = namedtuple('VirtualDrive',['drive_path', 'virtual_drive_name', 'raid_level', 'raid_size', 'drive_group', 'write_policy'])

# timeit decorator for, you know, timing testing
def timeit(method):
    def timed(*args, **kw):
        tstart = time.time()
        result = method(*args, **kw)
        tend = time.time()
        print('==> %r (%r, %r) %2.2f sec' % \
              (method.__name__, args, kw, tend-tstart))
        return result
    return timed

class InventoryDict(defaultdict):

    # pprint doesn't know how to handle defaultdict - it wants a dict __repr__.
    # Let's override its __repr__ method so that it prints out like a regular dict
    __repr__ = dict.__repr__

class UcsServer():

    version = Version(0,6,0)

    def __init__(self, ipaddress, username, password):
        self.session_cookie = None
        self.session_refresh_period = None
        self.status_message = ''
        self.ipaddress = ipaddress
        self.username = username
        self.password = password
        self.serial_no = 'not queried'
        self.model = 'not queried'
        self.total_memory = 0
        self.inventory = InventoryDict()

    def __enter__(self):
        if self.login():
            return self

    def __exit__(self, exc_type, exc_inst, exc_tb):
        if exc_type is not None:
            print('%s' % exc_inst.args[0])
            # print '%s' % exc_tb.__dict__
            return True
        # print 'Returning None which is a false value, meaning, no execeptions were handled'
        self.logout()

    # @timeit
    def login(self):
        """
        Log in to the CIMC using the instance's ipaddress, username, and password configured during init()

        XML Query:
        <aaaLogin inName='admin' inPassword='password'></aaaLogin>" -X POST https://172.29.85.36/nuova --insecure
        XML Response:
        <aaaLogin cookie="" response="yes" outCookie="1394044707/539306f8-f3e0-13e0-8005-1af7ea354e4c" outRefreshPeriod="600"
            outPriv="admin" outSessionId="43" outVersion="1.5(4)"> </aaaLogin>

        """
        command_string = "<aaaLogin inName='%s' inPassword='%s'></aaaLogin>" % (self.username, self.password)
        try:
            with RemapExceptions():
                response = post_request(self.ipaddress, command_string, timeout=LOGIN_TIMEOUT)
                if 'outCookie' in response.attrib:
                    self.session_cookie = response.attrib['outCookie']
                if 'outRefreshPeriod' in response.attrib:
                    self.session_refresh_period = response.attrib['outRefreshPeriod']
                if 'outVersion' in response.attrib:
                    self.version = response.attrib['outVersion']
            return self
        except TimeoutError:
            print('Timeout connecting to %s' % self.ipaddress)
            sys.exit()
        except ConnectionError as err:
            print('Could not connect to %s: %s' % (self.ipaddress, err))
            sys.exit()
        except ResponseError as err:
            print('Could not connect to %s: %s' % (self.ipaddress, err))
            sys.exit()

    # @timeit
    def logout(self):
        """
        Log out of the server instance. Invalidates the current session cookie in self.session_cookie
        """
        command_string = "<aaaLogout cookie='%s' inCookie='%s'></aaaLogout>" % (self.session_cookie, self.session_cookie)
        auth_response = post_request(self.ipaddress, command_string)

        if 'errorCode' in auth_response:
            self.status_message = "Logout Error: Server returned status code %s: %s" % (auth_response['errorCode'], auth_response['errorDescr'])
            raise Exception

    def set_power_state(self, power_state, force=False):
        """
        Change the power state of the server.

        power_state options from the XML Schema are
                "up", "down", "soft-shut-down", "cycle-immediate",
                "hard-reset-immediate", bmc-reset-immediate",
                "bmc-reset-default", "cmos-reset-immediate",
                "diagnostic-interrupt"
        """
        if force:
            command_string = '''<configConfMo cookie="%s" dn="sys/rack-unit-1" inHierarchical="false">\
            <inConfig>\
            <computeRackUnit dn="sys/rack-unit-1" adminPower="%s"></computeRackUnit>
            </inConfig>\
            </configConfMo>''' % (self.session_cookie, power_state)
            response_element = post_request(self.ipaddress, command_string)
            return True
        else:
            print('power() must be called with "force=True" to change the power status of the server')
            return False

    def refresh_cookie(self):
        pass

    def get_chassis_info(self):
        """
        Get the top-level chassis info and record useful info like serial number, model, memory, etc, in server.inventory['chassis'] sub-dictionary
        """
        chassis_dict = {}
        with RemapExceptions():
            command_string = '<configResolveClass cookie="%s" inHierarchical="false" classId="computeRackUnit"/>' % self.session_cookie
            response_element = post_request(self.ipaddress, command_string)
            for key,value in response_element.find('.//computeRackUnit').items():
                chassis_dict[key] = value
            self.inventory['chassis'] = chassis_dict
            self.serial_no = self.inventory['chassis']['serial']
            self.model = self.inventory['chassis']['model']
            self.total_memory = self.inventory['chassis']['totalMemory']
            self.name = self.inventory['chassis']['name']
            self.operPower = self.inventory['chassis']['operPower']
            return self

    def get_cimc_info(self):
        with RemapExceptions():
            command_string = '<configResolveChildren cookie="%s" inHierarchical="true" inDn="sys/rack-unit-1/mgmt"/>' % self.session_cookie
            response_element = post_request(self.ipaddress, command_string)
            out_configs = response_element.find('outConfigs')
            self.inventory['cimc'] = out_configs.find('mgmtIf').attrib

    def get_boot_order(self):
        bootorder_dict = {}
        with RemapExceptions():
            command_string = '<configResolveChildren cookie="%s" inHierarchical="false" inDn="sys/rack-unit-1/boot-policy"/>' % self.session_cookie
            response_element = post_request(self.ipaddress, command_string)
            out_configs = response_element.find('outConfigs')
            for i in out_configs.getchildren():
                print(i)
                try:
                    bootorder_dict[i.attrib['order']] = i.attrib['type']
                except:
                    self.inventory['boot_order'] = None
                    return self

            # represent the boot order as an ordered list from the returned dict based on the 'order' key
            #   {'1': 'virtual-media', '3': 'storage', '2': 'lan'} becomes ['virtual-media', 'lan', 'storage']
            self.inventory['boot_order'] = [bootorder_dict[key] for key in sorted(bootorder_dict)]
            return self

    def get_drive_inventory(self):
        """
        Retrieve both physical and virtual drive inventories.
        Populate <instance>.inventory['drives'] with the resulting dictionary
        """
        drive_dict = {'storageLocalDisk':[], 'storageVirtualDrive':[]}
        command_string = ['<configResolveClass cookie="%s" inHierarchical="false" classId="storageLocalDisk"/>' % self.session_cookie,
                          '<configResolveClass cookie="%s" inHierarchical="false" classId="storageVirtualDrive"/>' % self.session_cookie]
        with RemapExceptions():
            for command in command_string:
                response_element = post_request(self.ipaddress, command)
                out_configs = response_element.find('outConfigs')
                for config in out_configs.getchildren():
                    drive_dict[config.tag].append(config.attrib)
            self.inventory['drives'] = drive_dict
            return self

    def get_local_drive_usage(self):
        local_drive_usage_list=[]
        command_string = '<configResolveClass cookie="%s" inHierarchical="false" classId="storageLocalDiskUsage"/>' % self.session_cookie
        response_element = post_request(self.ipaddress, command_string)
        out_configs = response_element.find('outConfigs')
        for config in out_configs.getchildren():
            local_drive_usage_list.append(config.attrib)
        self.inventory['drive_usage'] = local_drive_usage_list
        return self

    def configure_pd_as_unconfigured_good_from_jbod(self, controller_path, phys_drive_id, force=False):
        """
        <configConfMo cookie='$REPLACE_ACTUAL_COOKIE_VALUE' inHierarchical='true' dn='sys/rack-unit-1/board/storage-SAS-SLOT-4/pd-8'>
            <inConfig>
                <storageLocalDisk dn='sys/rack-unit-1/board/storage-SAS-SLOT-4/pd-8' id='8'
                adminAction='make-unconfigured-good'/>
            </inConfig>
        </configConfMo>
        """
        if force:
            command_string = '''<configConfMo cookie="%s" inHierarchical="true" dn="%s/pd-%s">
            <inConfig>
                <storageLocalDisk dn="%s/pd-%s" id='%s'
                adminAction='make-unconfigured-good'/>
            </inConfig>
        </configConfMo>''' % (self.session_cookie, controller_path, phys_drive_id, controller_path, phys_drive_id, phys_drive_id)
            print('will execute %s' % command_string)
            # Just printing out for now. Don't actually execute the command
            #  response_element = post_request(self.ipaddress, command_string, timeout=CREATE_DRIVE_TIMEOUT)
        else:
            print('configure_pd_as_unconfigured_good_from_jbod() must be called with "force=True" to force to JBOD')
            return False


    def print_drive_inventory(self):
        """
        Print out the drive inventory dict in a user-friendly format.
        """
        if any(self.inventory['drives']):
            print('Virtual Drives:')
            for vd in self.inventory['drives']['storageVirtualDrive']:
                print("{id:>2} {dn:<48} {size:>11}  {raidLevel}  {name}".format(**vd))
            print('Physical Drives:')
            for pd in self.inventory['drives']['storageLocalDisk']:
                print("{id:>2} {dn:<48} {coercedSize:>11}  {pdStatus}".format(**pd))
        else:
            print('No drive inventory found! Please run "get_drive_inventory() on the server instance first.')

    @timeit
    def create_virtual_drive(self, controller_path, virtual_drive_name, raid_level, raid_size, drive_group, write_policy='Write Back Good BBU', force=False, debug=False):
        """
        <configConfMo cookie='$REPLACE_ACTUAL_COOKIE_VALUE' inHierarchical='false' dn='sys/rack-unit-1/board/storage-SAS-SLOT-2/virtual-drive-create'>
           <inConfig>
              <storageVirtualDriveCreatorUsingUnusedPhysicalDrive dn='sys/rack-unit-1/board/storage-SAS-SLOT-2/virtual-drive-create'
               virtualDriveName='RAID0_5'
               raidLevel='0'
               size='952720 MB'
               driveGroup='[5]'
               writePolicy='Write Back Good BBU'
               adminState='trigger'/>
           </inConfig>
        </configConfMo>
        """
        if force:
            command_string = '''<configConfMo cookie="%s" inHierarchical="false" dn="%s/virtual-drive-create">
               <inConfig>
                  <storageVirtualDriveCreatorUsingUnusedPhysicalDrive dn="%s/virtual-drive-create"
                   virtualDriveName="%s"
                   raidLevel="%s"
                   size="%s"
                   driveGroup="[%s]"
                   writePolicy="%s"
                   adminState="trigger"/>
               </inConfig>
            </configConfMo>''' % (self.session_cookie, controller_path, controller_path, virtual_drive_name, raid_level, raid_size, drive_group, write_policy)
            if debug:
                print('XML Drive create command:',command_string)
            response_element = post_request(self.ipaddress, command_string, timeout=CREATE_DRIVE_TIMEOUT)
            return True
        else:
            print('create_virtual_drive() must be called with "force=True" to create the drive')
            return False

    def get_interface_inventory(self):
        """
        Get network interface inventory with three calls:
            query adaptorUnit classId to find all adaptors
            query adaptorExtEthIf classId to find all physical network interfaces
            query adaptorHostEthIf classId to find all vNIC interfaces
        Combine all of the results in a hierarchical dict structure and return it in self.inventory['interfaces']
        """

        adaptorUnit_list = []
        adaptorHostEthIf_list = []
        adaptorExtEthIf_list = []
        with RemapExceptions():
            # query adaptorUnit classId to find all adaptors
            #  {'dn': 'sys/rack-unit-1/adaptor-2', 'cimcManagementEnabled': 'no', 'vendor': 'Cisco Systems Inc', 'description': '', 'presence': 'equipped', 'model': 'UCSC-PCIE-CSC-02', 'adminState': 'policy', 'pciSlot': '2', 'pciAddr': '64', 'serial': 'FCH17457FSM', 'id': '2'}
            #  {'dn': 'sys/rack-unit-1/adaptor-5', 'cimcManagementEnabled': 'no', 'vendor': 'Cisco Systems Inc', 'description': '', 'presence': 'equipped', 'model': 'UCSC-PCIE-CSC-02', 'adminState': 'policy', 'pciSlot': '5', 'pciAddr': '73', 'serial': 'FCH17457FUC', 'id': '5'}

            command_string = '<configResolveClass cookie="%s" inHierarchical="false" classId="%s"/>' %\
                             (self.session_cookie, 'adaptorUnit')
            response_element = post_request(self.ipaddress, command_string)
            out_configs = response_element.find('outConfigs')
            for config in out_configs.getchildren():
                adaptorUnit_list.append(config.attrib)
            #self.inventory['adaptor'] = adaptorUnit_list

            # query adaptorExtEthIf classId to find all physical network interfaces
            command_string = '<configResolveClass cookie="%s" inHierarchical="false" classId="%s"/>' %\
                             (self.session_cookie, 'adaptorExtEthIf')
            response_element = post_request(self.ipaddress, command_string)
            out_configs = response_element.find('outConfigs')
            for config in out_configs.getchildren():
                adaptorExtEthIf_list.append(config.attrib)
            #self.inventory['ext_eth_if'] = adaptorExtEthIf_list

            # query adaptorHostEthIf classId to find all vNIC interfaces
            command_string = '<configResolveClass cookie="%s" inHierarchical="false" classId="%s"/>' %\
                             (self.session_cookie, 'adaptorHostEthIf')
            response_element = post_request(self.ipaddress, command_string)
            out_configs = response_element.find('outConfigs')
            for config in out_configs.getchildren():
                adaptorHostEthIf_list.append(config.attrib)
            #self.inventory['host_eth_if'] = adaptorHostEthIf_list

        # Build a nested JSON structure with adaptor, physical ports, and vnics
        out_list = []
        for adaptor in adaptorUnit_list:
            # create an empty list of ports for each adaptor
            if 'port' not in adaptor:
                adaptor['port'] = []
            for port in adaptorExtEthIf_list:
                # create an empty list of vnics for each port
                if 'vnic' not in port:
                    port['vnic'] = []
                # If this port is on the current adaptor, append its dict to the 'port' list
                if adaptor['dn'].split('/')[2] == port['dn'].split('/')[2]:
                    adaptor['port'].append(port)
                    for vnic in adaptorHostEthIf_list:
                        # If this vnic is on the current adaptor and is also on the current port,
                        #  append it to the port's vnic list
                        if (adaptor['dn'].split('/')[2] == vnic['dn'].split('/')[2]) and (vnic.get('uplinkPort') == port['portId']):
                            port['vnic'].append(vnic)

            out_list.append(adaptor)

        self.inventory['adaptor'] = out_list

    def get_pci_inventory(self):
        """
        Query the pciEquipSlot class to get all PCI cards
        pciEquipSlot : {'dn': 'sys/rack-unit-1/equipped-slot-2', 'smbiosId': '2', 'controllerReported': '2', 'vendor': '0x1137', 'model': 'UCS VIC 1225 10Gbps 2 port CNA SFP+', 'id': '2'}
        pciEquipSlot : {'dn': 'sys/rack-unit-1/equipped-slot-4', 'smbiosId': '4', 'controllerReported': '4', 'vendor': '0x1000', 'model': 'LSI 9271-8i MegaRAID SAS HBA', 'id': '4'}
        pciEquipSlot : {'dn': 'sys/rack-unit-1/equipped-slot-5', 'smbiosId': '5', 'controllerReported': '5', 'vendor': '0x1137', 'model': 'UCS VIC 1225 10Gbps 2 port CNA SFP+', 'id': '5'}
        """

        pciEquipSlot_list = []
        with RemapExceptions():
            command_string = '<configResolveClass cookie="%s" inHierarchical="false" classId="pciEquipSlot"/>' % self.session_cookie
            response_element = post_request(self.ipaddress, command_string)
            out_configs = response_element.find('outConfigs')
            for config in out_configs.getchildren():
                pciEquipSlot_list.append(config.attrib)
            self.inventory['pci'] = pciEquipSlot_list

    def get_psu_inventory(self):
        """
        Query the equipmentPsu class to get the power supply inventory and status on the server
        Populate <instance>.inventory['ps'] with the resulting dictionary

        Example XMLAPI response:
        <configResolveClass cookie="1399390892/150e0b80-f8bd-18bd-8009-2678d0f1d3e4" response="yes" classId="equipmentPsu">
            <outConfigs>
                <equipmentPsu id="1" model="UCSC-PSU-650W" operability="operable" power="on" presence="equipped" serial="LIT162304LU" thermal="ok" vendor="Cisco Systems Inc" voltage="ok" dn="sys/rack-unit-1/psu-1" ></equipmentPsu>
                <equipmentPsu id="2" model="" operability="unknown" power="off" presence="missing" serial="" thermal="unknown" vendor="" voltage="unknown" dn="sys/rack-unit-1/psu-2" ></equipmentPsu>
            </outConfigs>
        </configResolveClass>
        """

        psu_list = []
        with RemapExceptions():
            command_string = '<configResolveClass cookie="%s" inHierarchical="false" classId="equipmentPsu"/>' % self.session_cookie
            response_element = post_request(self.ipaddress, command_string)
            out_configs = response_element.find('outConfigs')
            for config in out_configs.getchildren():
                psu_list.append(config.attrib)
            self.inventory['psu'] = psu_list

    def get_bios_settings(self):
        """
        Query the firmwareRunning class to get all FW versions on the server
        Populate <instance>.inventory['bios'] with the resulting dictionary
        """
        with RemapExceptions():
            bios_dict = {}
            command_string = '<configResolveClass cookie="%s" inHierarchical="true" classId="biosSettings"/>' % self.session_cookie
            response_element = post_request(self.ipaddress,command_string)
            all_bios_settings = response_element.find('*/biosSettings').getchildren()
            for i in all_bios_settings:
                bios_dict[i.attrib['rn']] = {}
                for key,value in i.items():
                    if key != 'rn':
                      bios_dict[i.attrib['rn']][key]=value
            self.inventory['bios'] = bios_dict

    def set_bios_custom(self):
        """
        Set the BIOS settings to Cisco's recommendations for virtualization
        """
        with RemapExceptions():
            command_string = configConfMo_prepend_string % self.session_cookie
            for item in config.CUSTOM_BIOS_SETTINGS:
                command_string += configConfMo_template.format(item=item)
            command_string += configConfMo_append_string
            response_element = post_request(self.ipaddress, command_string)

    def set_sol_adminstate(self, state='enable', speed='115200', comport='com0'):
        """
        Change the admin state of the Serial over LAN feature. Valid states are 'enable' and 'disable'.
        Valid speeds are '115200', '57600', '38400', '19200', '9600'
        Valid COM ports are 'com0' and 'com1'
        """
        command_string = '<configConfMo cookie="%s" inHierarchical="false" dn="sys/rack-unit-1/sol-if">\
                            <inConfig><solIf adminState="%s" speed="%s" comport="%s"></solIf>\
                            </inConfig></configConfMo>' % (self.session_cookie, state, speed, comport)
        with RemapExceptions():
            response_element = post_request(self.ipaddress, command_string)
            print('Changed SOL admin state to', state)

    def get_users(self):

        command_string = '<configResolveClass cookie="%s" inHierarchical="false" classId="aaaUser"/>' % self.session_cookie
        with RemapExceptions():
            response_element = post_request(self.ipaddress, command_string)
            self.inventory['users'] =  [user.attrib for user in response_element.findall('*/aaaUser')
                    if user.attrib['name']]

    def set_password(self, userid, password):
        """<configConfMo cookie="<cookie>" inHierarchical="false" dn="sys/user-ext/user-3">
                <inConfig>
                    <aaaUser id="3" pwd="<new_password>" />
                </inConfig>
            </configConfMo>"""
        if not self.inventory['users']:
            self.get_users()
        # Make sure we have the requested user
        try:
            (id, dn) = next((user['id'],user['dn'])
                            for user in self.inventory['users']
                            if user['name'] == userid)
        except StopIteration:
            print('Cannot find user', userid)
            return False

        # ready to go. Change the user password
        command_string = '<configConfMo cookie="%s" inHierarchical="false" dn="%s">\
            <inConfig> <aaaUser id="%s" pwd="%s" /> </inConfig> </configConfMo>' % (self.session_cookie, dn, id, password)
        with RemapExceptions():
            response_element = post_request(self.ipaddress, command_string)

    # @timeit
    def get_fw_versions(self):
        """
        Query the firmwareRunning class to get all FW versions on the server
        Populate <instance>.inventory['fw'] with the resulting sorted list
        """
        fw_dict = {}
        command_string = '<configResolveClass cookie="%s" inHierarchical="false" classId="firmwareRunning"/>' % self.session_cookie
        with RemapExceptions():
            response_element = post_request(self.ipaddress,command_string)
            for i in response_element.iter('firmwareRunning'):
                # ignore elements with 'fw-boot-loader'. More detail than we care about
                # we just want 'fw-system' entries
                if 'fw-boot-loader' not in i.attrib['dn']:
                    fw_dict[i.attrib['dn']] = i.attrib['version']
            self.inventory['fw'] = fw_dict
            return self

def post_request(server, command_string, timeout=REQUEST_TIMEOUT):
    url = "https://%s/nuova" % server
    try:
        with RemapExceptions():
            response = ET.fromstring(requests.post(url, data=command_string, verify=False, timeout=timeout).text)
            # print 'response.attrib:', response.attrib
            # If something went wrong, the response will have an 'errorCode' key
            # if so, then print the error message and raise an exception
            if 'errorCode' in response.keys():
                print('command:', command_string)
                print('response.attrib:', response.attrib)
                raise ResponseError("'%s': '%s'" % (response.attrib['errorCode'], response.attrib['errorDescr']))
            else:
                return response
    except TimeoutError:
        print('Timed out communicating with %s' % server)
        sys.exit()
    # except ConnectionError:
    #     print 'Network problem connecting to %s' % server
    #     sys.exit()


if __name__ == "__main__":
    IPADDR = '192.168.1.1'
    USERNAME = 'admin'
    PASSWORD = 'test123'

    import sys


    # Test the set_sol_adminstate() method
    if 0:
        with UcsServer(IPADDR, USERNAME, PASSWORD) as server:
            server.set_sol_adminstate('enable')

    if 0:
        with UcsServer(IPADDR, USERNAME, PASSWORD) as server:
            server.get_fw_versions()
            out_string = server.ipaddress + ','
            for key,value in server.inventory['fw'].items():
                # lop off the first two elements in the path since they're the same for all responses
                path = '/'.join(key.split('/')[2:])
                out_string += path + ',' + value + ','
            print(out_string)

    if 0:
        with UcsServer(IPADDR,USERNAME,PASSWORD) as server:
            print('== chassis info ==')
            server.get_chassis_info()
            print('== CIMC info ==')
            server.get_cimc_info()
            # print '== Boot order =='
            # myserver.get_boot_order()
            print('== Drive inventory ==')
            server.get_drive_inventory()
            print('== FW versions ==')
            server.get_fw_versions()
            print('== BIOS settings ==')
            server.get_bios_settings()
            print('== PCI inventory ==')
            server.get_pci_inventory()
            print('== Interface inventory ==')
            server.get_interface_inventory()
            print('== PSU inventory ==')
            server.get_psu_inventory()

            pprint(server.inventory)


    if 0:
        if server.login():
            for item,command in command_strings.items():
                start = time.time()
                full_command = command % server.session_cookie
                response = post_request(server.ipaddress, full_command)
                print(item)
                tmp = response.find('outConfigs').getchildren()
                for item in tmp:
                    print(item.tag, item.attrib)
                print(time.time() - start)
                print('\n')

            server.logout()
