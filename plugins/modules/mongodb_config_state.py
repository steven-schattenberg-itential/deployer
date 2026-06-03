#!/usr/bin/python

# Copyright (c) 2024, Itential, Inc
# GNU General Public License v3.0+ (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = r'''
---
module: mongodb_config_state

short_description: Report the configuration state of a running MongoDB instance.

version_added: "3.0.0"

description:
    - Connects to a MongoDB instance and returns boolean flags describing its
      current runtime configuration state.
    - Reports whether replication is active, whether a replica set has ever been
      initialized, and whether authentication is enforced.
    - This module is purely informational and makes no changes to the server.
    - Supports standalone instances, replica set members, and servers with TLS
      and/or authentication enabled.
    - When tls_enabled is true the module attempts a TLS connection first. If
      the TLS handshake fails (for example because the server is running in
      initialize stage without TLS) it automatically falls back to a plain
      connection, allowing the module to be called uniformly throughout the
      deployment sequence without knowing the current TLS state of the server.

options:
    login_database:
        description: The authentication database.
        required: false
        default: admin
        type: str
    login_host:
        description: The hostname or IP address of the MongoDB server.
        required: true
        type: str
    login_port:
        description: The port MongoDB is listening on.
        required: false
        default: 27017
        type: int
    tls_enabled:
        description:
            - When true, attempt to connect with TLS using the provided
              certificate files.
            - If the cert files do not exist on the target host, or if the TLS
              handshake fails, the module falls back to a plain connection.
        required: false
        default: false
        type: bool
    tls_ca_file:
        description: Path to the CA certificate file on the target host.
        required: false
        type: str
    tls_cert_key_file:
        description:
            - Path to the combined certificate and private key PEM file on the
              target host.
        required: false
        type: str

author:
    - Steven Schattenberg (@steven-schattenberg-itential)
'''

EXAMPLES = r'''
- name: Discover MongoDB configuration state (no TLS)
  itential.deployer.mongodb_config_state:
    login_host: mongo01.example.com
    login_port: 27017

- name: Discover MongoDB configuration state (TLS enabled)
  itential.deployer.mongodb_config_state:
    login_host: mongo01.example.com
    login_port: 27017
    tls_enabled: true
    tls_ca_file: /etc/pki/mongodb/ca-bundle.crt
    tls_cert_key_file: /etc/pki/mongodb/mongo01.example.com.pem
'''

RETURN = r'''
replication_enabled:
    description:
        - True if the running mongod process is actively participating in a
          replica set. Determined by the presence of setName in the hello
          command response.
        - This reflects runtime state only. A server restarted without
          replSetName in its config reports false even if replica set data
          exists on disk.
    type: bool
    returned: always
    sample: true
rs_configured:
    description:
        - True if local.system.replset contains a document, meaning a replica
          set was initialized at some point and its configuration is persisted
          on disk.
        - This can be true while replication_enabled is false when the server
          has been restarted in standalone mode (e.g. during the initialize
          stage of a re-run). The combination is used to decide whether to
          call rs.initiate() or rs.reconfig() during deployment.
    type: bool
    returned: always
    sample: true
auth_enabled:
    description:
        - True if the server is enforcing authentication. Determined by
          attempting an unauthenticated usersInfo command and treating an
          OperationFailure as confirmation that auth is required.
    type: bool
    returned: always
    sample: true
primary:
    description:
        - The hostname of the current replica set primary, without port.
        - Empty string when replication is not active or no primary has been
          elected yet.
    type: str
    returned: always
    sample: "mongo01.example.com"
members:
    description:
        - The list of replica set member hostnames including ports.
        - Empty list when replication is not active.
    type: list
    returned: always
    sample: ["mongo01.example.com:27017", "mongo02.example.com:27017"]
'''

import os

from ansible.module_utils.basic import AnsibleModule
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure


def build_connection_string(args):
    return "mongodb://" + args["login_host"] + ":" + str(args["login_port"]) + "/" + args["login_database"]


def run_module():
    module_args = dict(
        login_database=dict(type='str', required=False, default='admin'),
        login_host=dict(type='str', required=True),
        login_port=dict(type='int', required=False, default=27017),
        tls_enabled=dict(type='bool', required=False, default=False),
        tls_ca_file=dict(type='str', required=False, default=None),
        tls_cert_key_file=dict(type='str', required=False, default=None)
    )

    result = dict(
        changed=False,
        replication_enabled=False,
        rs_configured=False,
        auth_enabled=False,
        primary="",
        members=[]
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    if module.check_mode:
        module.exit_json(**result)

    uri = build_connection_string(module.params)

    # Only activate TLS if both cert files are present on the target host.
    # During a fresh install the files do not exist yet; during a re-run they
    # exist from the previous deployment but the server may temporarily be
    # running without TLS (initialize stage).
    client_kwargs = {}
    if module.params['tls_enabled']:
        ca_file = module.params['tls_ca_file']
        cert_key_file = module.params['tls_cert_key_file']
        if ca_file and os.path.exists(ca_file) and cert_key_file and os.path.exists(cert_key_file):
            client_kwargs['tls'] = True
            client_kwargs['tlsCAFile'] = ca_file
            client_kwargs['tlsCertificateKeyFile'] = cert_key_file

    # Use a short serverSelectionTimeoutMS on the first attempt. A TLS
    # handshake either succeeds in milliseconds or fails immediately with EOF;
    # the default 30-second timeout would cause a long delay on every re-run
    # before the TLS fallback could fire.
    try:
        client = MongoClient(uri, **client_kwargs, serverSelectionTimeoutMS=5000)
        database = client.get_database("admin")
        hello = database.command("hello")
    except ConnectionFailure:
        if client_kwargs.get('tls'):
            # The server rejected the TLS handshake. This is expected when the
            # deployer restarts mongod in initialize stage (no TLS) on a host
            # that already has cert files from a previous deployment. Connect
            # without TLS so the module can still report configuration state.
            client = MongoClient(uri)
            database = client.get_database("admin")
            hello = database.command("hello")
        else:
            raise

    # rs_configured reflects whether a replica set has ever been initialized on
    # this host. It is true even when the server is currently running as a
    # standalone (replication_enabled=false), which happens after every
    # initialize-stage restart during re-runs. The deployer uses this to decide
    # whether to call rs.initiate() (fresh install) or rs.reconfig() (re-run).
    result["rs_configured"] = client["local"]["system.replset"].count_documents({}) > 0

    # setName is only present in the hello response when the server is actively
    # participating in a replica set. primary and hosts may be absent during an
    # election, so both are guarded before access.
    if "setName" in hello:
        result["replication_enabled"] = True
        if "primary" in hello:
            result["primary"] = hello["primary"].split(":")[0]
        if "hosts" in hello:
            result["members"] = hello["hosts"]

    # usersInfo requires an authenticated connection. By connecting without
    # credentials and catching the resulting OperationFailure we can determine
    # whether auth is enforced without needing to know any passwords.
    try:
        database.command('usersInfo')
    except OperationFailure:
        result["auth_enabled"] = True

    module.exit_json(**result)


def main():
    run_module()


if __name__ == '__main__':
    main()
