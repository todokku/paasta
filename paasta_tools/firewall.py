# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import unicode_literals

import collections
import hashlib
import itertools
import json

import six

from paasta_tools import iptables
from paasta_tools.chronos_tools import load_chronos_job_config
from paasta_tools.marathon_tools import get_all_namespaces_for_service
from paasta_tools.marathon_tools import load_marathon_service_config
from paasta_tools.utils import load_system_paasta_config


PRIVATE_IP_RANGES = (
    '127.0.0.0/255.0.0.0',
    '10.0.0.0/255.0.0.0',
    '172.16.0.0/255.240.0.0',
    '192.168.0.0/255.255.0.0',
    '169.254.0.0/255.255.0.0',
)


class ServiceGroup(collections.namedtuple('ServiceGroup', (
    'service',
    'instance',
    'framework',
    'soa_dir',
))):
    """A service group.

    :param service: service name
    :param instance: instance name
    :param framework: Mesos framework (e.g. marathon or chronos)
    :param soa_dir: path to yelpsoa-configs
    """

    __slots__ = ()

    @property
    def chain_name(self):
        """Return iptables chain name.

        Chain names are limited to 28 characters, so we have to trim quite a
        bit. To attempt to ensure we don't have collisions due to shortening,
        we append a hash to the end.
        """
        chain = 'PAASTA.{}'.format(self.service[:10])
        chain += '.' + hashlib.sha256(
            json.dumps(self).encode('utf8'),
        ).hexdigest()[:10]
        assert len(chain) <= 28, len(chain)
        return chain

    @property
    def config(self):
        load_fn = {
            'chronos': load_chronos_job_config,
            'marathon': load_marathon_service_config,
        }[self.framework]
        return load_fn(
            self.service, self.instance,
            load_system_paasta_config().get_cluster(),
            load_deployments=False,
            soa_dir=self.soa_dir,
        )

    @property
    def rules(self):
        conf = self.config

        rules = [_default_rule(conf)]
        rules.extend(_well_known_rules(conf))
        rules.extend(_smartstack_rules(conf, self.soa_dir))
        return tuple(rules)

    def update_rules(self):
        iptables.ensure_chain(self.chain_name, self.rules)


def _default_rule(conf):
    policy = conf.get_outbound_firewall()
    if policy == 'block':
        return iptables.Rule(
            protocol='ip',
            src='0.0.0.0/0.0.0.0',
            dst='0.0.0.0/0.0.0.0',
            target='REJECT',
            matches=(),
        )
    elif policy == 'monitor':
        # TODO: log-prefix
        return iptables.Rule(
            protocol='ip',
            src='0.0.0.0/0.0.0.0',
            dst='0.0.0.0/0.0.0.0',
            target='LOG',
            matches=(),
        )
    else:
        raise AssertionError(policy)


def _well_known_rules(conf):
    for resource in conf.get_dependencies().get('well-known', ()):
        if resource == 'internet':
            yield iptables.Rule(
                protocol='ip',
                src='0.0.0.0/0.0.0.0',
                dst='0.0.0.0/0.0.0.0',
                target='PAASTA-INTERNET',
                matches=(),
            )
        else:
            # TODO: handle better
            raise KeyError(resource)


def _smartstack_rules(conf, soa_dir):
    for namespace in conf.get_dependencies().get('smartstack', ()):
        # TODO: handle non-synapse-haproxy services
        # TODO: support wildcards?
        service, _ = namespace.split('.', 1)
        service_namespaces = get_all_namespaces_for_service(service, soa_dir=soa_dir)
        port = dict(service_namespaces)[namespace]['proxy_port']

        yield iptables.Rule(
            protocol='tcp',
            src='0.0.0.0/0.0.0.0',
            dst='169.254.255.254/255.255.255.255',
            target='ACCEPT',
            matches=(
                ('tcp', (('dport', six.text_type(port)),)),
            )
        )


def active_service_groups(soa_dir):
    """Return active service groups."""
    from paasta_tools import firewall_update
    service_groups = collections.defaultdict(set)
    for service, instance, framework, mac in firewall_update.services_running_here(soa_dir):
        service_groups[ServiceGroup(service, instance, framework, soa_dir)].add(mac)
    return service_groups


def ensure_internet_chain():
    iptables.ensure_chain(
        'PAASTA-INTERNET',
        (
            iptables.Rule(
                protocol='ip',
                src='0.0.0.0/0.0.0.0',
                dst='0.0.0.0/0.0.0.0',
                target='ACCEPT',
                matches=(),
            ),
        ) + tuple(
            iptables.Rule(
                protocol='ip',
                src='0.0.0.0/0.0.0.0',
                dst=ip_range,
                target='RETURN',
                matches=(),
            )
            for ip_range in PRIVATE_IP_RANGES
        )
    )


def ensure_service_chains(soa_dir):
    """Ensure service chains exist and have the right rules.

    Returns dictionary {[service chain] => [list of mac addresses]}.
    """
    chains = {}
    for service, macs in active_service_groups(soa_dir).items():
        service.update_rules()
        chains[service.chain_name] = macs
    return chains


def ensure_dispatch_chains(service_chains):
    paasta_rules = set(itertools.chain.from_iterable(
        (
            iptables.Rule(
                protocol='ip',
                src='0.0.0.0/0.0.0.0',
                dst='0.0.0.0/0.0.0.0',
                target=chain,
                matches=(
                    ('mac', (('mac_source', mac.upper()),)),
                ),

            )
            for mac in macs
        )
        for chain, macs in service_chains.items()

    ))
    iptables.ensure_chain('PAASTA', paasta_rules)

    jump_to_paasta = iptables.Rule(
        protocol='ip',
        src='0.0.0.0/0.0.0.0',
        dst='0.0.0.0/0.0.0.0',
        target='PAASTA',
        matches=(),
    )
    iptables.ensure_rule('INPUT', jump_to_paasta)
    iptables.ensure_rule('FORWARD', jump_to_paasta)


def garbage_collect_old_service_chains(desired_chains):
    current_paasta_chains = {
        chain
        for chain in iptables.all_chains()
        if chain.startswith('PAASTA.')
    }
    for chain in current_paasta_chains - set(desired_chains):
        iptables.delete_chain(chain)


def general_update(soa_dir):
    """Update iptables to match the current PaaSTA state."""
    ensure_internet_chain()
    service_chains = ensure_service_chains(soa_dir)
    ensure_dispatch_chains(service_chains)
    garbage_collect_old_service_chains(service_chains)
