# vim: tabstop=4 shiftwidth=4 softtabstop=4

#Copyright 2013 Cloudbase Solutions SRL
#Copyright 2013 Pedro Navarro Perez
#All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
# @author: Pedro Navarro Perez
# @author: Alessandro Pilotti, Cloudbase Solutions Srl

import eventlet
import platform
import re
import sys
import time

from oslo.config import cfg

from quantum.agent import rpc as agent_rpc
from quantum.common import config as logging_config
from quantum.common import topics
from quantum import context
from quantum.openstack.common import log as logging
from quantum.openstack.common.rpc import dispatcher
from quantum.plugins.hyperv.agent import utils
from quantum.plugins.hyperv.common import constants

LOG = logging.getLogger(__name__)

agent_opts = [
    cfg.ListOpt(
        'physical_network_vswitch_mappings',
        default=[],
        help=_('List of <physical_network>:<vswitch> '
        'where the physical networks can be expressed with '
        'wildcards, e.g.: ."*:external"')),
    cfg.StrOpt(
        'local_network_vswitch',
        default='private',
        help=_('Private vswitch name used for local networks')),
    cfg.IntOpt('polling_interval', default=2,
               help=_("The number of seconds the agent will wait between "
                      "polling for local device changes.")),
]


CONF = cfg.CONF
CONF.register_opts(agent_opts, "AGENT")


class HyperVQuantumAgent(object):
    # Set RPC API version to 1.0 by default.
    RPC_API_VERSION = '1.0'

    def __init__(self):
        self._utils = utils.HyperVUtils()
        self._polling_interval = CONF.AGENT.polling_interval
        self._load_physical_network_mappings()
        self._network_vswitch_map = {}
        self._setup_rpc()

    def _setup_rpc(self):
        self.agent_id = 'hyperv_%s' % platform.node()
        self.topic = topics.AGENT
        self.plugin_rpc = agent_rpc.PluginApi(topics.PLUGIN)

        # RPC network init
        self.context = context.get_admin_context_without_session()
        # Handle updates from service
        self.dispatcher = self._create_rpc_dispatcher()
        # Define the listening consumers for the agent
        consumers = [[topics.PORT, topics.UPDATE],
                     [topics.NETWORK, topics.DELETE],
                     [topics.PORT, topics.DELETE],
                     [constants.TUNNEL, topics.UPDATE]]
        self.connection = agent_rpc.create_consumers(self.dispatcher,
                                                     self.topic,
                                                     consumers)

    def _load_physical_network_mappings(self):
        self._physical_network_mappings = {}
        for mapping in CONF.AGENT.physical_network_vswitch_mappings:
            parts = mapping.split(':')
            if len(parts) != 2:
                LOG.debug(_('Invalid physical network mapping: %s'), mapping)
            else:
                pattern = re.escape(parts[0].strip()).replace('\\*', '.*')
                vswitch = parts[1].strip()
                self._physical_network_mappings[re.compile(pattern)] = vswitch

    def _get_vswitch_for_physical_network(self, phys_network_name):
        for compre in self._physical_network_mappings:
            if phys_network_name is None:
                phys_network_name = ''
            if compre.match(phys_network_name):
                return self._physical_network_mappings[compre]
        # Not found in the mappings, the vswitch has the same name
        return phys_network_name

    def _get_network_vswitch_map_by_port_id(self, port_id):
        for network_id, map in self._network_vswitch_map.iteritems():
            if port_id in map['ports']:
                return (network_id, map)

    def network_delete(self, context, network_id=None):
        LOG.debug(_("network_delete received. "
                    "Deleting network %s"), network_id)
        # The network may not be defined on this agent
        if network_id in self._network_vswitch_map:
            self._reclaim_local_network(network_id)
        else:
            LOG.debug(_("Network %s not defined on agent."), network_id)

    def port_delete(self, context, port_id=None):
        LOG.debug(_("port_delete received"))
        self._port_unbound(port_id)

    def port_update(self, context, port=None, network_type=None,
                    segmentation_id=None, physical_network=None):
        LOG.debug(_("port_update received"))
        self._treat_vif_port(
            port['id'], port['network_id'],
            network_type, physical_network,
            segmentation_id, port['admin_state_up'])

    def _create_rpc_dispatcher(self):
        return dispatcher.RpcDispatcher([self])

    def _get_vswitch_name(self, network_type, physical_network):
        if network_type != constants.TYPE_LOCAL:
            vswitch_name = self._get_vswitch_for_physical_network(
                physical_network)
        else:
            vswitch_name = CONF.AGENT.local_network_vswitch
        return vswitch_name

    def _provision_network(self, port_id,
                           net_uuid, network_type,
                           physical_network,
                           segmentation_id):
        LOG.info(_("Provisioning network %s"), net_uuid)

        vswitch_name = self._get_vswitch_name(network_type, physical_network)

        if network_type == constants.TYPE_VLAN:
            self._utils.add_vlan_id_to_vswitch(segmentation_id, vswitch_name)
        elif network_type == constants.TYPE_FLAT:
            self._utils.set_vswitch_mode_access(vswitch_name)
        elif network_type == constants.TYPE_LOCAL:
            #TODO (alexpilotti): Check that the switch type is private
            #or create it if not existing
            pass
        else:
            raise utils.HyperVException(_("Cannot provision unknown network "
                                          "type %s for network %s"),
                                        network_type, net_uuid)

        map = {
            'network_type': network_type,
            'vswitch_name': vswitch_name,
            'ports': [],
            'vlan_id': segmentation_id}
        self._network_vswitch_map[net_uuid] = map

    def _reclaim_local_network(self, net_uuid):
        LOG.info(_("Reclaiming local network %s"), net_uuid)
        map = self._network_vswitch_map[net_uuid]

        if map['network_type'] == constants.TYPE_VLAN:
            LOG.info(_("Reclaiming VLAN ID %s "), map['vlan_id'])
            self._utils.remove_vlan_id_from_vswitch(
                map['vlan_id'], map['vswitch_name'])
        else:
            raise utils.HyperVException(_("Cannot reclaim unsupported "
                                          "network type %s for network %s"),
                                        map['network_type'], net_uuid)

        del self._network_vswitch_map[net_uuid]

    def _port_bound(self, port_id,
                    net_uuid,
                    network_type,
                    physical_network,
                    segmentation_id):
        LOG.debug(_("Binding port %s"), port_id)

        if net_uuid not in self._network_vswitch_map:
            self._provision_network(
                port_id, net_uuid, network_type,
                physical_network, segmentation_id)

        map = self._network_vswitch_map[net_uuid]
        map['ports'].append(port_id)

        self._utils.connect_vnic_to_vswitch(map['vswitch_name'], port_id)

        if network_type == constants.TYPE_VLAN:
            LOG.info(_('Binding VLAN ID %s to switch port %s'),
                     segmentation_id, port_id)
            self._utils.set_vswitch_port_vlan_id(
                segmentation_id,
                port_id)
        elif network_type == constants.TYPE_FLAT:
            #Nothing to do
            pass
        elif network_type == constants.TYPE_LOCAL:
            #Nothing to do
            pass
        else:
            LOG.error(_('Unsupported network type %s'), network_type)

    def _port_unbound(self, port_id):
        (net_uuid, map) = self._get_network_vswitch_map_by_port_id(port_id)
        if net_uuid not in self._network_vswitch_map:
            LOG.info(_('Network %s is not avalailable on this agent'),
                     net_uuid)
            return

        LOG.debug(_("Unbinding port %s"), port_id)
        self._utils.disconnect_switch_port(map['vswitch_name'], port_id, True)

        if not map['ports']:
            self._reclaim_local_network(net_uuid)

    def _update_ports(self, registered_ports):
        ports = self._utils.get_vnic_ids()
        if ports == registered_ports:
            return
        added = ports - registered_ports
        removed = registered_ports - ports
        return {'current': ports,
                'added': added,
                'removed': removed}

    def _treat_vif_port(self, port_id, network_id, network_type,
                        physical_network, segmentation_id,
                        admin_state_up):
        if self._utils.vnic_port_exists(port_id):
            if admin_state_up:
                self._port_bound(port_id, network_id, network_type,
                                 physical_network, segmentation_id)
            else:
                self._port_unbound(port_id)
        else:
            LOG.debug(_("No port %s defined on agent."), port_id)

    def _treat_devices_added(self, devices):
        resync = False
        for device in devices:
            LOG.info(_("Adding port %s") % device)
            try:
                device_details = self.plugin_rpc.get_device_details(
                    self.context,
                    device,
                    self.agent_id)
            except Exception as e:
                LOG.debug(_(
                    "Unable to get port details for device %s: %s"),
                    device, e)
                resync = True
                continue
            if 'port_id' in device_details:
                LOG.info(_(
                    "Port %(device)s updated. Details: %(device_details)s") %
                    locals())
                self._treat_vif_port(
                    device_details['port_id'],
                    device_details['network_id'],
                    device_details['network_type'],
                    device_details['physical_network'],
                    device_details['segmentation_id'],
                    device_details['admin_state_up'])
        return resync

    def _treat_devices_removed(self, devices):
        resync = False
        for device in devices:
            LOG.info(_("Removing port %s"), device)
            try:
                self.plugin_rpc.update_device_down(self.context,
                                                   device,
                                                   self.agent_id)
            except Exception as e:
                LOG.debug(_("Removing port failed for device %s: %s"),
                          device, e)
                resync = True
                continue
            self._port_unbound(device)
        return resync

    def _process_network_ports(self, port_info):
        resync_a = False
        resync_b = False
        if 'added' in port_info:
            resync_a = self._treat_devices_added(port_info['added'])
        if 'removed' in port_info:
            resync_b = self._treat_devices_removed(port_info['removed'])
        # If one of the above operations fails => resync with plugin
        return (resync_a | resync_b)

    def daemon_loop(self):
        sync = True
        ports = set()

        while True:
            try:
                start = time.time()
                if sync:
                    LOG.info(_("Agent out of sync with plugin!"))
                    ports.clear()
                    sync = False

                port_info = self._update_ports(ports)

                # notify plugin about port deltas
                if port_info:
                    LOG.debug(_("Agent loop has new devices!"))
                    # If treat devices fails - must resync with plugin
                    sync = self._process_network_ports(port_info)
                    ports = port_info['current']
            except Exception as e:
                LOG.exception(_("Error in agent event loop: %s"), e)
                sync = True

            # sleep till end of polling interval
            elapsed = (time.time() - start)
            if (elapsed < self._polling_interval):
                time.sleep(self._polling_interval - elapsed)
            else:
                LOG.debug(_("Loop iteration exceeded interval "
                            "(%(polling_interval)s vs. %(elapsed)s)"),
                          {'polling_interval': self._polling_interval,
                           'elapsed': elapsed})


def main():
    eventlet.monkey_patch()
    cfg.CONF(project='quantum')
    logging_config.setup_logging(cfg.CONF)

    plugin = HyperVQuantumAgent()

    # Start everything.
    LOG.info(_("Agent initialized successfully, now running... "))
    plugin.daemon_loop()
    sys.exit(0)
