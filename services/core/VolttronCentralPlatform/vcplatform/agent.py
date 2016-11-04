# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:

# Copyright (c) 2016, Battelle Memorial Institute
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in
#    the documentation and/or other materials provided with the
#    distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation
# are those of the authors and should not be interpreted as representing
# official policies, either expressed or implied, of the FreeBSD
# Project.
#
# This material was prepared as an account of work sponsored by an
# agency of the United States Government.  Neither the United States
# Government nor the United States Department of Energy, nor Battelle,
# nor any of their employees, nor any jurisdiction or organization that
# has cooperated in the development of these materials, makes any
# warranty, express or implied, or assumes any legal liability or
# responsibility for the accuracy, completeness, or usefulness or any
# information, apparatus, product, software, or process disclosed, or
# represents that its use would not infringe privately owned rights.
#
# Reference herein to any specific commercial product, process, or
# service by trade name, trademark, manufacturer, or otherwise does not
# necessarily constitute or imply its endorsement, recommendation, or
# favoring by the United States Government or any agency thereof, or
# Battelle Memorial Institute. The views and opinions of authors
# expressed herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY
# operated by BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830
# }}}


from __future__ import absolute_import, print_function

import base64
from copy import deepcopy
from collections import namedtuple
import datetime
from enum import Enum
import hashlib
import logging
import re
import shutil
import sys
import tempfile
import urlparse

import gevent
import gevent.event
from gevent.lock import BoundedSemaphore
import psutil
import requests
from volttron.platform import get_home
from volttron.platform.agent.utils import (
    get_aware_utc_now, format_timestamp, parse_timestamp_string,
    get_utc_seconds_from_epoch)
from volttron.platform.messaging import topics
from volttron.platform.messaging.topics import (LOGGER, PLATFORM_VCP_DEVICES,
                                                PLATFORM)
from volttron.platform.vip.agent.subsystems.query import Query
from volttron.platform.vip.agent.connection import Connection

from volttron.platform.vip.agent import *

from volttron.platform import jsonrpc
from volttron.platform.agent import utils
from volttron.platform.agent.exit_codes import INVALID_CONFIGURATION_CODE
from volttron.platform.agent.known_identities import (
    VOLTTRON_CENTRAL, VOLTTRON_CENTRAL_PLATFORM)
from volttron.platform.agent.utils import (
    get_aware_utc_now, format_timestamp, parse_timestamp_string)
from volttron.platform.auth import AuthEntry, AuthFile
from volttron.platform.jsonrpc import (INTERNAL_ERROR, INVALID_PARAMS)
from volttron.platform.keystore import KnownHostsStore
from volttron.platform.messaging.health import Status, \
    GOOD_STATUS, BAD_STATUS
from volttron.platform.messaging.topics import (LOGGER, PLATFORM_VCP_DEVICES,
                                                PLATFORM)
from volttron.platform.vip.agent import *
from volttron.platform.vip.agent.connection import Connection
from volttron.platform.vip.agent.subsystems.query import Query
from volttron.platform.vip.agent.utils import build_connection
from volttron.platform.web import DiscoveryInfo
from volttron.utils.persistance import load_create_store

__version__ = '3.6.0'

utils.setup_logging()
_log = logging.getLogger(__name__)

# After setup logging
from bacnet_proxy_reader import BACnetReader
_log.debug('LOGGING SETUP?')


class NotManagedError(StandardError):
    """ Raised if vcp cannot connect to the vc trying to manage it.

    Some examples of this could be if the serverkey is not valid, if the
    tcp address is invalid, if the http address is invalid.

    Other examples could be permissions issues from auth.
    """
    pass


RegistrationStates = Enum('AgentStates',
                               'NotRegistered Unregistered Registered '
                               'Registering')


class VolttronCentralPlatform(Agent):
    __name__ = 'VolttronCentralPlatform'

    def __init__(self, config_path, **kwargs):
        super(VolttronCentralPlatform, self).__init__(**kwargs)

        config = utils.load_config(config_path)

        vc_reconnect_interval = config.get(
            'volttron-central-reconnect-interval', 5)
        vc_address = config.get('volttron-central-address')
        vc_serverkey = config.get('volttron-central-serverkey')
        instance_name = config.get('instance-name')
        stats_publish_interval = config.get('stats-publish-interval', 30)
        topic_replace_map = config.get('topic-replace-map', {})

        # This is scheduled after first call to the reconnect function
        self._scheduled_connection_event = None
        self._publish_bacnet_iam = False

        # default_configuration is what is specified if there isn't a "config"
        # sent in through the volttron-ctl config store command.
        self.default_config = dict(
            volttron_central_reconnect_interval=vc_reconnect_interval,
            volttron_central_address=vc_address,
            volttron_central_serverkey=vc_serverkey,
            instance_name=instance_name,
            stats_publish_interval=stats_publish_interval,
            topic_replace_map=topic_replace_map,
            local_serverkey=None,
            local_external_addresses=None
        )

        # current_config can be used an manipulated at runtime, while
        # default_config is passed to the config_store as the defaults
        self.current_config = None

        # Start using config store.
        self.vip.config.set_default("default_config", self.default_config)
        self.vip.config.subscribe(self.configure_main,
                                  actions=["NEW", "UPDATE", "DELETE"],
                                  pattern="default_config")
        self.vip.config.subscribe(self.configure_main,
                                  actions=["NEW", "UPDATE", "DELETE"],
                                  pattern="config")
        self.vip.config.subscribe(self.configure_platform,
                                  actions=["NEW", "UPDATE", "DELETE"],
                                  pattern="platform")

        # Allows the periodic check for registration.  It is set to true after
        # a main configuration is changed.
        self.enable_registration = False

        # A connection to the volttron central agent.
        self.vc_connection = None

        # This publickey is set during the manage rpc call.
        self.manager_publickey = None

        self.registration_state = RegistrationStates.NotRegistered

        # # This flag will allow us to change the state of the connection
        # # to volttron central.
        # self._should_attempt_connection = False
        #
        # # In the constructor we don't know yet whether the agent is registered
        # # or not to volttron central.
        # self.agent_state = AgentRegistrationStates.Unregistered
        #
        # # The connection to the volttron.central agent on the vc instance.
        # self.volttron_central_connection = None
        #
        # # A connection to the local control service instance.
        # self.control_connection = None
        #
        # self.local_bind_web_address = None
        # self.external_addresses = None
        #
        # # Setup devices tree in the config store.
        # self.devices = {}
        # self.device_topic_hashes = {}
        #
        # self.topic_replace_map = {}
        #
        # # Default publish interval to 20 seconds.
        # self.stats_publish_interval = 30
        # self._stats_publisher = None


        #
        # # These variables are initially set here from the configuration
        # # file that is packaged with the agent.  These variables will be
        # # overwritten in the configuration_main function when those values
        # # are updated.
        # self._volttron_central_address = vc_address
        # self._volttron_central_serverkey = vc_serverkey
        # self.instance_name = instance_name



    def configure_platform(self, config_name, action, contents):
        pass

    def configure_main(self, config_name, action, contents):
        """
        This is the main configuration point for the agent.

        :param config_name:
        :param action:
        :param contents:
        :return:
        """
        self.enable_registration = False

        if config_name == 'default_config':
            # we know this came from the config file that was specified
            # with the agent
            config = self.default_config.copy()

        elif config_name == 'config':
            config = self.default_config.copy()
            config.update(contents)

        else:
            _log.error('Invalid configuration name!')
            sys.exit(INVALID_CONFIGURATION_CODE)

        _log.debug('Querying router for addresses and serverkey.')
        q = Query(self.core)

        external_addresses = q.query('addresses').get(timeout=5)
        local_serverkey = q.query('serverkey').get(timeout=5)
        vc_address = q.query('volttron-central-address').get(timeout=5)
        vc_serverkey = q.query('volttron-central-serverkey').get(timeout=5)
        instance_name = q.query('instance-name').get(timeout=5)
        instance_id = hashlib.md5(external_addresses[0]).hexdigest()

        updates = dict(
            volttron_central_address=vc_address,
            volttron_central_serverkey=vc_serverkey,
            instance_name=instance_name,
            instance_id=instance_id,
            local_serverkey=local_serverkey,
            local_external_addresses=external_addresses
        )

        if config_name == 'default_config':
            for k, v in updates.items():
                if v:
                    config[k] = v
        elif config_name == 'config':
            for k, v in updates.items():
                # Only update from the platform's configuration file if the
                # value doesn't exist in the config or if it is empty.
                if k not in config:
                    config[k] = v
                elif not config[k]:
                    config[k] = v

        self.current_config = config.copy()

        vc_address = self.current_config['volttron_central_address']
        vc_serverkey = self.current_config['volttron_central_serverkey']

        parsed = urlparse.urlparse(vc_address)

        if parsed.scheme in ('http', 'https'):
            _log.debug('vc_address is {}'.format(vc_address))
            info = DiscoveryInfo.request_discovery_info(vc_address)
            self.current_config['vc_connect_address'] = info.vip_address
            self.current_config['vc_connect_serverkey'] = info.serverkey
        else:
            self.current_config['vc_connect_address'] = vc_address
            self.current_config['vc_connect_serverkey'] = vc_serverkey

        _log.info("Current configuration is: {}".format(self.current_config))

        self.enable_registration = True
        self._periodic_attempt_registration()
        # if action == "NEW":
        #     _log.debug('Querying router for addresses and serverkey.')
        #     q = Query(self.core)
        #
        #     external_addresses = q.query('addresses').get(timeout=5)
        #     _log.debug('External addresses are: {}'.format(
        #         external_addresses))
        #
        #     local_serverkey = q.query('serverkey').get(timeout=5)
        #     _log.debug('serverkey is: {}'.format(local_serverkey))
        #
        #     vc_address = q.query('volttron-central-address').get(timeout=5)
        #     _log.debug('vc address is {}'.format(vc_address))
        #
        #     vc_serverkey = q.query('volttron-central-serverkey').get(timeout=5)
        #     _log.debug('vc serverkey is {}'.format(vc_serverkey))
        #
        #     instance_name = q.query('instance-name').get(timeout=5)
        #     _log.debug('instance name is {}'.format(instance_name))
        #
        #     instance_id = hashlib.md5(external_addresses[0]).hexdigest()
        #     _log.debug('instance id is: {}'.format(instance_id))
        #
        #     # Update the current_config with the newest information from
        #     # the store.
        #     self.current_config.update(contents)
        #
        #     # now overwrite things from the platform config because it will take
        #     # precidence over what is sent with the agent.
        #     if not vc_address:
        #         self.current_config['volttron-central-address'] = vc_address
        #     if not vc_serverkey:
        #         self.current_config['volttron-central-serverkey'] = vc_serverkey
        #     if not instance_name:
        #         self.current_config['instance-name'] = instance_name
        #
        #     if self.volttron_central_connection is not None and \
        #             self.volttron_central_connection.is_connected:
        #         self.volttron_central_connection.kill()
        #         self.volttron_central_connection = None
        #     self.current_config['instance_id'] = instance_id
        #
        #     # Created a link to the control agent on this platform.
        #     self.control_connection = build_connection(
        #         identity='vcp.to.control', peer='control',
        #         reconnect_interval=5000)
        #
        #     assert self.control_connection.is_connected(3)
        #
        # elif action == 'UPDATE':
        #     _log.debug('Contents: {}'.format(contents))
        #     self.current_config.update(contents)
        #
        # else:  # DELETE - Resets back to the original default configuration
        #     self.current_config = self.default_config.copy()
        #
        # #        try:
        # vc_address = self.current_config['volttron-central-address']
        # if vc_address is None:
        #     self.vip.health.set_status(BAD_STATUS,
        #                                "Invalid volttron-central-address "
        #                                "during configuration.")
        #     _log.warn("Invalid volttron-central-address")
        #     return
        #
        # address_type = self._address_type(vc_address)
        #
        # if address_type in ('https', 'http'):
        #     info = DiscoveryInfo.request_discovery_info(vc_address)
        #     self._volttron_central_address = info.vip_address
        #     self._volttron_central_serverkey = info.serverkey
        # elif address_type in ('tcp'):
        #     self._volttron_central_address = vc_address
        #     self._volttron_central_serverkey = vc_serverkey
        #
        # self._reconnect_to_vc()
        # except ValueError as e:
        #     context = "INVALID Configuration please update " +\
        #               "'volttron-central-address' in configuration. " +\
        #               "set to: {}".format(vc_address)
        #
        #     _log.error(context)
        #     key = "INVALID_VC_ADDRESS"
        #     status = Status.build(BAD_STATUS, context)
        #     self.vip.health.send_alert(key, status)

    @RPC.export
    def get_public_keys(self):
        """
        RPC method to retrieve all of the public keys fo the installed agents.

        :return: A mapping of identity to publickey.
        :rtype: dict
        """
        return self.control_connection.call('get_agents_publickeys')


    def _address_type(self, address):
        """
        Parses the passed address and return it's scheme if it is one of the
        correct values otherwise throw a ValueError

        :param address: The address to be checked.
        :return: The scheme of the address
        """
        parsed_type = None
        parsed = urlparse.urlparse(address)
        if parsed.scheme not in ('http', 'https', 'ipc', 'tcp'):
            raise ValueError('Invalid volttron central address.')

        return parsed.scheme

    def _reconnect_to_vc(self):
        # with self.vcl_semaphore:
        if self.volttron_central_connection is not None and \
                self.volttron_central_connection.is_connected:
            self.volttron_central_connection.kill()
            self.volttron_central_connection = None

        instance_id = self.current_config['instance_id']
        vc_address = self._volttron_central_address
        vc_serverkey = self._volttron_central_serverkey

        _log.debug('Connecting using vc: {} serverkey: {}'.format(vc_address,
                                                                  vc_serverkey))

        self.volttron_central_connection = build_connection(
            identity=instance_id, peer=VOLTTRON_CENTRAL, address=vc_address,
            serverkey=vc_serverkey, publickey=self.core.publickey,
            secretkey=self.core.secretkey
        )

        assert self.volttron_central_connection.is_connected()
        assert self.volttron_central_connection.is_peer_connected()

    def _update_vcp_config(self, external_addresses, local_serverkey,
                           vc_address, vc_serverkey, instance_name):
        assert external_addresses

        # This is how the platform will be referred to on vc.
        md5 = hashlib.md5(external_addresses[0])
        local_instance_id = md5.hexdigest()

        # If we get an http address then we need to look up the serverkey and
        # vip-address from volttorn central
        if self._address_type(vc_address) in ('http', 'https'):
            info = DiscoveryInfo.request_discovery_info(vc_address)
            assert info
            assert info.vip_address
            assert info.serverkey

            config = dict(vc_serverkey=info.serverkey,
                          vc_vip_address=info.vip_address,
                          vc_agent_publickey=None,
                          local_instance_name=instance_name,
                          local_instance_id=local_instance_id,
                          local_external_address=external_addresses[0],
                          local_serverkey=local_serverkey
                          )
        else:
            config = dict(vc_serverkey=vc_serverkey,
                          vc_vip_address=vc_address,
                          vc_agent_publickey=None,
                          local_instance_name=instance_name,
                          local_instance_id=local_instance_id,
                          local_external_address=external_addresses[0],
                          local_serverkey=local_serverkey
                          )
        # Store the config parameters in the config store for later use.
        self.vip.config.set("vc-conn-config", config)

    @RPC.export
    def reconfigure(self, **kwargs):
        # self.vip.config.set("config", kwargs)
        _log.debug('AFTER CCCCONNFIG STOREEEEEEE')
        # Update config store with newest address info.
        # self.vip.config.set("config", self._vc_conn_configuration._asdict())

        # instance_name = kwargs.get('instance-name')
        # instance_uuid = kwargs.get('instance-uuid')
        # vc_address = kwargs.get('volttron-central-address')
        # vc_serverkey = kwargs.get('volttron-central-serverkey')
        # new_publish_interval = kwargs.get('stats-publish-interval')
        # reconnect_interval = kwargs.get('volttron-central-reconnect-interval')
        #
        # if instance_name:
        #     self._local_instance_name = instance_name
        #
        # if instance_uuid:
        #     self._local_instance_uuid = instance_uuid
        #
        # if reconnect_interval:
        #     self._volttron_central_reconnect_interval = reconnect_interval
        #
        # if vc_address:
        #     parsed = urlparse.urlparse(vc_address)
        #     if parsed.scheme in ('http', 'https'):
        #         self._volttron_central_http_address = vc_address
        #     elif parsed.scheme == 'tcp':
        #         self._volttron_central_tcp_address = vc_address
        #     elif parsed.scheme == 'ipc':
        #         self._volttron_central_ipc_address = vc_address
        #     else:
        #         raise ValueError('Invalid volttron central address.')
        #
        # if vc_serverkey:
        #     if self._volttron_central_tcp_address:
        #         self._volttron_central_serverkey = vc_serverkey
        #     else:
        #         raise ValueError('Invalid volttron central tcp address.')
        #
        # if new_publish_interval is not None:
        #     if int(self._stats_publish_interval) < 20:
        #         raise ValueError(
        #             "stats publishing must be greater than 20 seconds.")
        #     self._stats_publish_interval = new_publish_interval
        #     self._start_stats_publisher()

    def _periodic_attempt_registration(self):

        if self._scheduled_connection_event is not None:
            # This won't hurt anything if we are canceling ourselves.
            self._scheduled_connection_event.cancel()

        if not self.enable_registration:
            now = get_aware_utc_now()
            next_update_time = now + datetime.timedelta(
                seconds=10)

            self._scheduled_connection_event = self.core.schedule(
                next_update_time, self._periodic_attempt_registration)
            return

        try:
            vc = self.get_vc_connection()
            if vc is None:
                _log.debug("vc not connected")
                return

            if self.registration_state == RegistrationStates.NotRegistered:
                _log.debug('Not registred beginning registration process.')
                _log.debug('Retrieving publickey from vc agent.')
                vc_agent_publickey = vc.call("get_publickey")
                _log.debug('vc agent publickey is {}'.format(
                    vc_agent_publickey))
                assert vc_agent_publickey and len(vc_agent_publickey) == 43
                _log.debug('Adding vc publickey to auth')
                entry = AuthEntry(credentials=vc_agent_publickey,
                                  capabilities=['manager'],
                                  comments="Added by VCP",
                                  user_id="vc")
                authfile = AuthFile()
                authfile.add(entry)

                local_name = self.current_config.get('local_instance_name')
                local_address = self.current_config.get(
                    'local_external_addresses')[0]
                local_serverkey = self.current_config.get('local_serverkey')
                vc_address = self.current_config.get('volttron_central_address')

                _log.debug("Registering with vc from vcp.")
                _log.debug("Instance is named: {}".format(local_name))
                _log.debug("Address is: {}".format(local_address))
                _log.debug("VC Address is: {}".format(vc_address))

                vc.call('register_instance', address=local_address,
                        display_name=local_name, vcpserverkey=local_serverkey,
                        vcpagentkey=self.core.publickey)

            else:
                _log.debug("Current platform registration state: {}".format(
                    self.registration_state))
        except Unreachable as e:
            _log.error("Couldn't connect to volttron.central. {}".format(
                self.current_config.get('volttron_central_address')
            ))
        except ValueError as e:
            _log.error(e.message)
        except Exception as e:
            _log.error("{} found as {}".format(e, e.message))
        except gevent.Timeout as e:
            _log.error("timout occured connecting to remote platform.")
        finally:
            _log.debug('Scheduling next periodic call')
            now = get_aware_utc_now()
            next_update_time = now + datetime.timedelta(
                seconds=10)

            self._scheduled_connection_event = self.core.schedule(
                next_update_time, self._periodic_attempt_registration)

    def get_vc_connection(self):
        """ Attempt to connect to volttron central management console.

        The attempts will be done in the following order.

        1. if peer is vc register with it.
        2. volttron-central-tcp and serverkey
        2. volttron-central-http (looks up tcp and serverkey)
        3. volttron-central-ipc

        :param sender:
        :param kwargs:
        :return:
        """

        if self.vc_connection:
            # if connected return the connection.
            if self.vc_connection.is_connected(5) and \
                    self.vc_connection.is_peer_connected(5):
                _log.debug('Returning current connection')
                return self.vc_connection

            _log.debug("Resetting connection as the peer wasn't responding.")
            # reset the connection so we can try it again below.
            self.vc_connection.kill()
            self.vc_connection = None

        # First check to see if there is a peer with a volttron.central
        # identity, if there is use it as the manager of the platform.
        peers = self.vip.peerlist().get(timeout=5)
        if VOLTTRON_CENTRAL in peers:
            _log.debug('VC is a local peer.')
            self.vc_connection = Connection(
                self.core.address, VOLTTRON_CENTRAL,
                publickey=self.core.publickey, secretkey=self.core.secretkey
            )
            if self.vc_connection.is_connected() and \
                    self.vc_connection.is_peer_connected():
                _log.debug("Connection has been established to local peer.")
            else:
                _log.error('Unable to connect to local peer!')
            return self.vc_connection

        if self.current_config.get('vc_connect_address') is None or \
                self.current_config.get('vc_connect_serverkey') is None:
            _log.warn('volttron_central_address is None in config store '
                      'and volttron.central is not a peer.')
            _log.warn('Recommend adding volttron.central address or adding a '
                      '"config" file to the config store.')
            return None

        c = self.current_config
        self.vc_connection = build_connection(
            identity=c.get('local_instance_name'),
            peer=VOLTTRON_CENTRAL,
            address=c.get('vc_connect_address'),
            serverkey=c.get('vc_connect_serverkey'),
            publickey=self.core.publickey,
            secretkey=self.core.secretkey
        )

        if not self.vc_connection.is_peer_connected():
            _log.error('Peer: {} is not connected to the external platform'
                       .format(self.vc_connection.peer))

        return self.vc_connection

        # # If we have an http address for volttron central, but haven't
        # # looked up the address yet, then look up and set the address from
        # # volttron central discovery.
        # if self._volttron_central_http_address is not None and \
        #                 self._volttron_central_tcp_address is None and \
        #                 self._volttron_central_serverkey is None:
        #
        #     _log.debug('Using discovery to lookup tcp connection')
        #
        #     response = requests.get(
        #         "{}/discovery/".format(self._volttron_central_http_address)
        #     )
        #
        #     if response.ok:
        #         jsonresp = response.json()
        #         entry = AuthEntry(credentials="/.*/",
        #                           capabilities=['manager']
        #                           #,
        #                           #address=jsonresp['vip-address']
        #                           )
        #         authfile = AuthFile(get_home() + "/auth.json")
        #         authfile.add(entry)
        #         self._volttron_central_tcp_address = jsonresp['vip-address']
        #         self._volttron_central_serverkey = jsonresp['serverkey']
        #
        # # First see if we are able to connect via tcp with the serverkey.
        # if self._volttron_central_tcp_address is not None and \
        #         self._volttron_central_serverkey is not None:
        #     _log.debug('Connecting to volttron central using tcp.')
        #
        #     vc_conn = Connection(
        #         address=self._volttron_central_tcp_address,
        #         peer=VOLTTRON_CENTRAL,
        #         serverkey=self._volttron_central_serverkey,
        #         publickey=self.core.publickey,
        #         secretkey=self.core.secretkey
        #     )
        #
        #     if not vc_conn.is_connected(5):
        #         raise ValueError(
        #             "Unable to connect to remote platform")
        #
        #     if not vc_conn.is_peer_connected(5):
        #         raise ValueError(
        #             "Peer: {} unavailable on remote platform.".format(
        #                 VOLTTRON_CENTRAL))
        #
        #     #TODO Only add a single time for this address.
        #     if self._volttron_central_publickey:
        #         # Add the vcpublickey to the auth file.
        #         entry = AuthEntry(
        #             credentials= self._volttron_central_publickey,
        #             capabilities=['manager'])
        #         authfile = AuthFile()
        #         authfile.add(entry)
        #
        #     self.volttron_central_connectionection = vc_conn

        #     return self.volttron_central_connectionection
        #
        # # Next see if we have a valid ipc address (Not Local though)
        # if self._volttron_central_ipc_address is not None:
        #     self.volttron_central_connectionection = Connection(
        #         address=self._volttron_central_ipc_address,
        #         peer=VOLTTRON_CENTRAL
        #     )
        #
        #     return self.volttron_central_connectionection

    def _start_stats_publisher(self):
        if not self._agent_started:
            return

        if self._stats_publisher:
            self._stats_publisher.kill()
        # The stats publisher publishes both to the local bus and the vc
        # bus the platform specific topics.
        self._stats_publisher = self.core.periodic(
            self._stats_publish_interval, self._publish_stats)

    @RPC.export
    def get_health(self):
        _log.debug("Getting health: {}".format(self.vip.health.get_status()))
        return self.vip.health.get_status()

    @RPC.export
    def get_instance_uuid(self):
        return self._local_instance_uuid

    @RPC.export
    @RPC.allow("manager")
    def get_publickey(self):
        return self.core.publickey

    @RPC.export
    @RPC.allow("manager")
    def manage(self, address):
        """ Allows the `VolttronCentralPlatform` to be managed.

        From the web perspective this should be after the user has specified
        that a user has blessed an agent to be able to be managed.

        When the user enters a discovery address in `VolttronCentral` it is
        implied that the user wants to manage a platform.

        :returns publickey of the `VolttronCentralPlatform`
        """
        _log.info('Manage request from address: {}'.format(address))

        if address != self.current_config['vc_connect_address']:
            _log.error("Managed by differeent volttron central.")
            return

        vc = self.get_vc_connection()

        if not vc.is_peer_connected():
            self.registration_state = RegistrationStates.NotRegistered
        else:
            self.registration_state = RegistrationStates.Registered

        return self.get_publickey()

    @RPC.export
    def publish_bacnet_props(self, proxy_identity, address, device_id,
                             filter=[]):

        bn = BACnetReader(self.vip.rpc, proxy_identity, self._bacnet_response)

        gevent.spawn(bn.read_device_properties, address, device_id, filter)

        return "PUBLISHING"

    def _bacnet_response(self, context, results):
        message=dict(results=results)
        if context is not None:
            message.update(context)
        gevent.spawn(self._pub_to_vc, "configure", message=message)


    @RPC.export
    def start_bacnet_scan(self, proxy_identity, low_device_id=None,
                          high_device_id=None, target_address=None,
                          scan_length=5):
        """This function is a wrapper around the bacnet proxy scan.
        """
        if proxy_identity not in self.vip.peerlist().get(timeout=5):
            raise Unreachable("Can't reach agent identity {}".format(
                proxy_identity))
        _log.info('Starting bacnet_scan with who_is request to {}'.format(
            proxy_identity))
        self.vip.rpc.call(proxy_identity, "who_is", low_device_id=low_device_id,
                          high_device_id=high_device_id,
                          target_address=target_address).get(timeout=5.0)
        timestamp = get_utc_seconds_from_epoch()
        self._publish_bacnet_iam = True
        self._pub_to_vc("iam", message=dict(status="STARTED IAM",
                                            timestamp=timestamp))

        def stop_iam():
            stop_timestamp = get_utc_seconds_from_epoch()
            self._pub_to_vc("iam", message=dict(
                status="FINISHED IAM",
                timestamp=stop_timestamp
            ))
            self._publish_bacnet_iam = False

        gevent.spawn_later(scan_length, stop_iam)


    @PubSub.subscribe('pubsub', topics.BACNET_I_AM)
    def _iam_handler(self, peer, sender, bus, topic, headers, message):
        if self._publish_bacnet_iam:
            proxy_identity = sender
            address = message['address']
            device_id = message['device_id']
            bn = BACnetReader(self.vip.rpc, proxy_identity)
            message['device_name'] = bn.read_device_name(address, device_id)
            message['device_description'] = bn.read_device_description(address,
                                                                       device_id)
            self._pub_to_vc("iam", message=message)

    def _pub_to_vc(self, topic_leaf, headers=None, message=None):
        vc = self.get_vc_connection()

        if not vc:
            _log.error('Platform must have connection to vc to publish {}'
                       .format(topic_leaf))
        else:
            topic = "platforms/{}/{}".format(self._local_instance_uuid,
                                                  topic_leaf)
            _log.debug('Publishing to vc topic: {}'.format(topic))
            vc.publish(topic=topic, headers=headers, message=message)


    @RPC.export
    def unmanage(self):
        pass
        # self._is_registering = False
        # self._is_registered = False
        # self._was_unmanaged = True

    @RPC.export
    # @RPC.allow("manager") #TODO: uncomment allow decorator
    def list_agents(self):
        """ List the agents that are installed on the platform.

        Note this only lists the agents that are actually installed on the
        instance.

        :return: A list of agents.
        """
        return self._get_agent_list()

    @RPC.export
    # @RPC.allow("can_manage")
    def start_agent(self, agent_uuid):
        self.control_connection.call("start_agent", agent_uuid)

    @RPC.export
    # @RPC.allow("can_manage")
    def stop_agent(self, agent_uuid):
        proc_result = self.control_connection.call("stop_agent", agent_uuid)

    @RPC.export
    # @RPC.allow("can_manage")
    def restart_agent(self, agent_uuid):
        self.control_connection.call("restart_agent", agent_uuid)
        gevent.sleep(0.2)
        return self.agent_status(agent_uuid)

    @RPC.export
    def agent_status(self, agent_uuid):
        return self.control_connection.call("agent_status", agent_uuid)

    @RPC.export
    def status_agents(self):
        return self.control_connection.call('status_agents')

    @RPC.export
    def get_device(self, topic):
        _log.debug('Get device for topic: {}'.format(topic))
        return self._devices.get(topic)

    @PubSub.subscribe('pubsub', 'devices')
    def _on_device_message(self, peer, sender, bus, topic, headers, message):
        # only deal with agents that have not been forwarded.
        if headers.get('X-Forwarded', None):
            return

        # only listen to the ending all message.
        if not re.match('.*/all$', topic):
            return

        topicsplit = topic.split('/')

        # For devices we use everything between devices/../all as a unique
        # key for determining the last time it was seen.
        key = '/'.join(topicsplit[1: -1])

        anon_topic = self._topic_replace_map.get(key)
        publish_time_utc = format_timestamp(get_aware_utc_now())

        if not anon_topic:
            anon_topic = key

            for sr in self._topic_replace_list:
                _log.debug(
                    'anon replacing {}->{}'.format(sr['from'], sr['to']))
                anon_topic = anon_topic.replace(sr['from'],
                                                sr['to'])
            _log.debug('anon after replacing {}'.format(anon_topic))
            _log.debug('Anon topic is: {}'.format(anon_topic))
            self._topic_replace_map[key] = anon_topic
            _log.debug('Only anon topics are being listed.')

        hashable = anon_topic + str(message[0].keys())
        _log.debug('Hashable is: {}'.format(hashable))
        md5 = hashlib.md5(hashable)
        # self._md5hasher.update(hashable)
        hashed = md5.hexdigest()

        self._device_topic_hashes[hashed] = anon_topic
        self._devices[anon_topic] = {
            'points': message[0].keys(),
            'last_published_utc': publish_time_utc,
            'md5hash': hashed
        }

        vc = self.get_vc_connection()
        if vc is not None:
            message = dict(md5hash=hashed, last_publish_utc=publish_time_utc)

            if self._local_instance_uuid is not None:
                vcp_topic = PLATFORM_VCP_DEVICES(
                    platform_uuid=self._local_instance_uuid,
                    topic=anon_topic
                )
                vc.publish(vcp_topic.format(), message=message)
            else:
                local_topic = PLATFORM(
                    subtopic="devices/{}".format(anon_topic))
                self.vip.pubsub.publish("pubsub", local_topic, message=message)

            _log.debug('Devices: {} Hashes: {} Platform: {}'.format(
                len(self._devices), self._device_topic_hashes,
                self._local_instance_name))

    @RPC.export
    def get_devices(self):
        cp = deepcopy(self._devices)
        foundbad = False

        for k, v in cp.items():
            dt = parse_timestamp_string(v['last_published_utc'])
            dtnow = get_aware_utc_now()
            if dt + datetime.timedelta(minutes=5) < dtnow:
                v['health'] = Status.build(
                    BAD_STATUS,
                    'Too long between publishes for {}'.format(k)).as_dict()
                foundbad = True
            else:
                v['health'] = Status.build(GOOD_STATUS).as_dict()

        if len(cp):
            if foundbad:
                self.vip.health.set_status(
                    BAD_STATUS,
                    'At least one device has not published in 5 minutes')
            else:
                self.vip.health.set_status(
                    GOOD_STATUS,
                    'All devices publishing normally.'
                )
        return cp

    @RPC.export
    def route_request(self, id, method, params):
        _log.debug(
            'platform agent routing request: {}, {}'.format(id, method))

        method_map = {
            'list_agents': self.list_agents,
            'get_devices': self.get_devices,
        }

        # First handle the elements that are going to this platform
        if method in method_map:
            result = method_map[method]()
        elif method == 'set_setting':
            result = self.set_setting(**params)
        elif method == 'get_setting':
            result = self.get_setting(**params)
        elif method == 'get_devices':
            result = self.get_devices()
        elif method == 'status_agents':
            _log.debug('Doing status agents')
            result = {'result': [{'name': a[1], 'uuid': a[0],
                                  'process_id': a[2][0],
                                  'return_code': a[2][1]}
                                 for a in
                                 self.control_connection.call(method)]}

        elif method in ('agent_status', 'start_agent', 'stop_agent',
                        'remove_agent', 'restart_agent'):
            _log.debug('We are trying to exectute method {}'.format(method))
            _log.debug('Params are: {}'.format(params))
            if isinstance(params, list) and len(params) != 1 or \
                            isinstance(params,
                                       dict) and 'uuid' not in params.keys():
                result = jsonrpc.json_error(ident=id, code=INVALID_PARAMS)
            else:
                if isinstance(params, list):
                    uuid = params[0]
                elif isinstance(params, str):
                    uuid = params
                else:
                    uuid = params['uuid']
                _log.debug('calling control with method: {} uuid: {}'.format(
                    method, uuid
                ))
                status = self.control_connection.call(method, uuid)
                if method == 'stop_agent' or status == None:
                    # Note we recurse here to get the agent status.
                    result = self.route_request(id, 'agent_status', uuid)
                else:
                    result = {'process_id': status[0],
                              'return_code': status[1]}
        elif method in ('install'):

            if not 'files' in params:
                result = jsonrpc.json_error(ident=id, code=INVALID_PARAMS)
            else:
                result = self._install_agents(params['files'])

        else:

            fields = method.split('.')

            if fields[0] == 'historian':
                if 'platform.historian' in self.vip.peerlist().get(timeout=30):
                    agent_method = fields[1]
                    result = self.vip.rpc.call('platform.historian',
                                               agent_method,
                                               **params).get(timeout=45)
                else:
                    result = jsonrpc.json_error(
                        id, INVALID_PARAMS, 'historian unavailable')
            else:
                agent_uuid = fields[2]
                agent_method = '.'.join(fields[3:])
                _log.debug("Calling method {} on agent {}"
                           .format(agent_method, agent_uuid))
                _log.debug("Params is: {}".format(params))
                if agent_method in ('start_bacnet_scan', 'publish_bacnet_props'):
                    identity = params.pop("proxy_identity")
                    if agent_method == 'start_bacnet_scan':
                        result = self.start_bacnet_scan(identity, **params)
                    elif agent_method == 'publish_bacnet_props':
                        result = self.publish_bacnet_props(identity, **params)
                else:
                    # find the identity of the agent so we can call it by name.
                    identity = self._control_connection.call('agent_vip_identity', agent_uuid)
                    if params:
                        if isinstance(params, list):
                            result = self.vip.rpc.call(identity, agent_method, *params).get(timeout=30)
                        else:
                            result = self.vip.rpc.call(identity, agent_method, **params).get(timeout=30)
                    else:
                        result = self.vip.rpc.call(identity, agent_method).get(timeout=30)
                # find the identity of the agent so we can call it by name.
                identity = self.control_connection.call('agent_vip_identity',
                                                        agent_uuid)
                if params:
                    if isinstance(params, list):
                        result = self.vip.rpc.call(identity, agent_method,
                                                   *params).get(timeout=30)
                    else:
                        result = self.vip.rpc.call(identity, agent_method,
                                                   **params).get(timeout=30)
                else:
                    result = self.vip.rpc.call(identity, agent_method).get(
                        timeout=30)

        if isinstance(result, dict):
            if 'result' in result:
                return result['result']
            elif 'code' in result:
                return result['code']
        elif result is None:
            return
        return result

    def _install_agents(self, agent_files):
        tmpdir = tempfile.mkdtemp()
        results = []

        for f in agent_files:
            try:
                if 'local' in f.keys():
                    path = f['file_name']
                else:
                    path = os.path.join(tmpdir, f['file_name'])
                    with open(path, 'wb') as fout:
                        fout.write(
                            base64.decodestring(f['file'].split('base64,')[1]))

                _log.debug('Calling control install agent.')
                uuid = self.vip.rpc.call('control', 'install_agent_local',
                                         path).get()

            except Exception as e:
                results.append({'error': str(e)})
                _log.error("EXCEPTION: " + str(e))
            else:
                results.append({'uuid': uuid})

        shutil.rmtree(tmpdir, ignore_errors=True)

        return results

    @RPC.export
    def list_agent_methods(self, method, params, id, agent_uuid):
        return jsonrpc.json_error(ident=id, code=INTERNAL_ERROR,
                                  message='Not implemented')

    def _publish_stats(self):
        """
        Publish the platform statistics to the local bus as well as to the
        connected volttron central.
        """
        vc_topic = None
        local_topic = LOGGER(subtopic="platform/status/cpu")
        _log.debug('Publishing platform cpu stats')
        if self._local_instance_uuid is not None:

            vc_topic = LOGGER(
                subtopic="platforms/{}/status/cpu".format(
                    self._local_instance_uuid))
            _log.debug('Stats will be published to: {}'.format(
                vc_topic.format()))
        else:
            _log.debug('Platform uuid is not valid')
        points = {}

        for k, v in psutil.cpu_times_percent().__dict__.items():
            points['times_percent/' + k] = {'Readings': v,
                                            'Units': 'double'}

        points['percent'] = {'Readings': psutil.cpu_percent(),
                             'Units': 'double'}
        try:
            vc = self.get_vc_connection()
            if vc is not None and vc.is_connected() and vc_topic is not None:
                vc.publish(vc_topic.format(), message=points)
        except Exception as e:
            _log.info("status not written to volttron central.")
        self.vip.pubsub.publish(peer='pubsub', topic=local_topic.format(),
                                message=points)

    @Core.receiver('onstop')
    def onstop(self, sender, **kwargs):
        if self.vc_connection is not None:
            self.vc_connection.kill()
            self.vc_connection = None
        if self.control_connection is not None:
            self.control_connection.kill()
            self.control_connection = None
            # self._is_registered = False
            # self._is_registering = False

    def _get_agent_list(self):
        """ Retrieve a list of agents on the platform.

        Each entry in the list

        :return: list: A list of agent data.
        """

        agents = self.control_connection.call("list_agents")
        status_running = self.status_agents()
        uuid_to_status = {}
        # proc_info has a list of [startproc, endprox]
        for a in agents:
            pinfo = None
            is_running = False
            for uuid, name, proc_info in status_running:
                if a['uuid'] == uuid:
                    is_running = proc_info[0] > 0 and proc_info[1] == None
                    pinfo = proc_info
                    break

            uuid_to_status[a['uuid']] = {
                'is_running': is_running,
                'process_id': None,
                'error_code': None,
                'permissions': {
                    'can_stop': is_running,
                    'can_start': not is_running,
                    'can_restart': True,
                    'can_remove': True
                }
            }

            if pinfo:
                uuid_to_status[a['uuid']]['process_id'] = proc_info[0]
                uuid_to_status[a['uuid']]['error_code'] = proc_info[1]

            if 'volttroncentral' in a['name'] or \
                            'vcplatform' in a['name']:
                uuid_to_status[a['uuid']]['permissions']['can_stop'] = False
                uuid_to_status[a['uuid']]['permissions']['can_remove'] = False

            # The default agent is stopped health looks like this.
            uuid_to_status[a['uuid']]['health'] = {
                'status': 'UNKNOWN',
                'context': None,
                'last_updated': None
            }

            if is_running:
                identity = self.vip.rpc.call('control', 'agent_vip_identity',
                                             a['uuid']).get(timeout=30)
                try:
                    status = self.vip.rpc.call(identity,
                                               'health.get_status').get(timeout=5)
                    uuid_to_status[a['uuid']]['health'] = status
                except gevent.Timeout:
                    _log.error("Couldn't get health from {} uuid: {}".format(
                        identity, a['uuid']
                    ))
                except Unreachable:
                    _log.error("Couldn't reach agent identity {} uuid: {}".format(
                        identity, a['uuid']
                    ))
        for a in agents:
            if a['uuid'] in uuid_to_status.keys():
                _log.debug('UPDATING STATUS OF: {}'.format(a['uuid']))
                a.update(uuid_to_status[a['uuid']])
        return agents


def main(argv=sys.argv):
    """ Main method called by the eggsecutable.
    :param argv:
    :return:
    """
    # utils.vip_main(platform_agent)
    utils.vip_main(VolttronCentralPlatform, identity=VOLTTRON_CENTRAL_PLATFORM)


if __name__ == '__main__':
    # Entry point for script
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
