#!/usr/bin/env python3
#
# Copyright (C) 2019-2021 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os

from glob import glob
from sys import exit

from vyos.config import Config
from vyos.configdict import get_interface_dict
from vyos.configverify import verify_address
from vyos.configverify import verify_dhcpv6
from vyos.configverify import verify_eapol
from vyos.configverify import verify_interface_exists
from vyos.configverify import verify_mirror
from vyos.configverify import verify_mtu
from vyos.configverify import verify_mtu_ipv6
from vyos.configverify import verify_vlan_config
from vyos.configverify import verify_vrf
from vyos.ethtool import Ethtool
from vyos.ifconfig import EthernetIf
from vyos.template import render
from vyos.util import call
from vyos.util import dict_search
from vyos import ConfigError
from vyos import airbag
airbag.enable()

# XXX: wpa_supplicant works on the source interface
wpa_suppl_conf = '/run/wpa_supplicant/{ifname}.conf'

def get_config(config=None):
    """
    Retrive CLI config as dictionary. Dictionary can never be empty, as at least the
    interface name will be added or a deleted flag
    """
    if config:
        conf = config
    else:
        conf = Config()
    base = ['interfaces', 'ethernet']
    ethernet = get_interface_dict(conf, base)

    return ethernet

def verify(ethernet):
    if 'deleted' in ethernet:
        return None

    ifname = ethernet['ifname']
    verify_interface_exists(ifname)

    ethtool = Ethtool(ifname)
    # No need to check speed and duplex keys as both have default values.
    if ((ethernet['speed'] == 'auto' and ethernet['duplex'] != 'auto') or
        (ethernet['speed'] != 'auto' and ethernet['duplex'] == 'auto')):
            raise ConfigError('Speed/Duplex missmatch. Must be both auto or manually configured')

    if ethernet['speed'] != 'auto' and ethernet['duplex'] != 'auto':
        # We need to verify if the requested speed and duplex setting is
        # supported by the underlaying NIC.
        speed = ethernet['speed']
        duplex = ethernet['duplex']
        if not ethtool.check_speed_duplex(speed, duplex):
            raise ConfigError(f'Adapter does not support changing speed and duplex '\
                              f'settings to: {speed}/{duplex}!')

    if 'ring_buffer' in ethernet:
        max_rx = ethtool.get_rx_buffer()
        if not max_rx:
            raise ConfigError('Driver does not support RX ring-buffer configuration!')

        max_tx = ethtool.get_tx_buffer()
        if not max_tx:
            raise ConfigError('Driver does not support TX ring-buffer configuration!')

        rx = dict_search('ring_buffer.rx', ethernet)
        if rx and int(rx) > int(max_rx):
            raise ConfigError(f'Driver only supports a maximum RX ring-buffer '\
                              f'size of "{max_rx}" bytes!')

        tx = dict_search('ring_buffer.tx', ethernet)
        if tx and int(tx) > int(max_tx):
            raise ConfigError(f'Driver only supports a maximum TX ring-buffer '\
                              f'size of "{max_tx}" bytes!')

    verify_mtu(ethernet)
    verify_mtu_ipv6(ethernet)
    verify_dhcpv6(ethernet)
    verify_address(ethernet)
    verify_vrf(ethernet)
    verify_eapol(ethernet)
    verify_mirror(ethernet)

    # verify offloading capabilities
    if dict_search('offload.rps', ethernet) != None:
        if not os.path.exists(f'/sys/class/net/{ifname}/queues/rx-0/rps_cpus'):
            raise ConfigError('Interface does not suport RPS!')

    driver = EthernetIf(ifname).get_driver_name()
    # T3342 - Xen driver requires special treatment
    if driver == 'vif':
        if int(ethernet['mtu']) > 1500 and dict_search('offload.sg', ethernet) == None:
            raise ConfigError('Xen netback drivers requires scatter-gatter offloading '\
                              'for MTU size larger then 1500 bytes')

    if {'is_bond_member', 'mac'} <= set(ethernet):
        print(f'WARNING: changing mac address "{mac}" will be ignored as "{ifname}" '
              f'is a member of bond "{is_bond_member}"'.format(**ethernet))

    # use common function to verify VLAN configuration
    verify_vlan_config(ethernet)
    return None

def generate(ethernet):
    if 'eapol' in ethernet:
        render(wpa_suppl_conf.format(**ethernet),
               'ethernet/wpa_supplicant.conf.tmpl', ethernet)
    else:
        # delete configuration on interface removal
        if os.path.isfile(wpa_suppl_conf.format(**ethernet)):
            os.unlink(wpa_suppl_conf.format(**ethernet))

    return None

def apply(ethernet):
    ifname = ethernet['ifname']
    # take care about EAPoL supplicant daemon
    eapol_action='stop'

    e = EthernetIf(ifname)
    if 'deleted' in ethernet:
        # delete interface
        e.remove()
    else:
        e.update(ethernet)
        if 'eapol' in ethernet:
            eapol_action='restart'

    call(f'systemctl {eapol_action} wpa_supplicant-macsec@{ifname}')

if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
