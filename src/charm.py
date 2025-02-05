#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charmed Operator for Tempo; a lightweight object storage based tracing backend."""

import logging
import re
import socket
from pathlib import Path
from typing import Optional, Tuple

import charms.tempo_k8s.v1.tracing as tracing_v1
import ops
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.grafana_k8s.v0.grafana_source import GrafanaSourceProvider
from charms.loki_k8s.v0.loki_push_api import LogProxyConsumer
from charms.observability_libs.v0.kubernetes_service_patch import KubernetesServicePatch
from charms.observability_libs.v1.cert_handler import CertHandler
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.tempo_k8s.v1.charm_tracing import trace_charm
from charms.tempo_k8s.v2.tracing import (
    ReceiverProtocol,
    RequestEvent,
    TracingEndpointProvider,
)
from charms.traefik_route_k8s.v0.traefik_route import TraefikRouteRequirer
from ops.charm import (
    CharmBase,
    CollectStatusEvent,
    HookEvent,
    PebbleNoticeEvent,
    RelationEvent,
    WorkloadEvent,
)
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus, Relation, WaitingStatus
from tempo import Tempo

logger = logging.getLogger(__name__)

LEGACY_RECEIVER_PROTOCOLS = (
    "otlp_grpc",
    "otlp_http",
    "zipkin",
)
"""Receiver protocol names supported by tracing v0/v1."""


@trace_charm(
    tracing_endpoint="tempo_otlp_http_endpoint",
    server_cert="server_cert",
    extra_types=(Tempo, TracingEndpointProvider, tracing_v1.TracingEndpointProvider),
)
class TempoCharm(CharmBase):
    """Charmed Operator for Tempo; a distributed tracing backend."""

    def __init__(self, *args):
        super().__init__(*args)
        self.tempo = tempo = Tempo(
            self.unit.get_container("tempo"),
            external_host=self.hostname,
            # we need otlp_http receiver for charm_tracing
            enable_receivers=["otlp_http"],
        )

        # TODO:
        #  ingress route provisioning a separate TCP ingress for each receiver if GRPC doesn't work directly
        self.ingress = TraefikRouteRequirer(self, self.model.get_relation("ingress"), "ingress")  # type: ignore

        self.cert_handler = CertHandler(
            self,
            key="tempo-server-cert",
            sans=[self.hostname],
        )

        # configure this tempo as a datasource in grafana
        self.grafana_source_provider = GrafanaSourceProvider(
            self,
            source_type="tempo",
            source_url=self._external_http_server_url,
            refresh_event=[
                # refresh the source url when TLS config might be changing
                self.on[self.cert_handler.certificates_relation_name].relation_changed,
                # or when ingress changes
                self.ingress.on.ready,
            ],
        )
        # # Patch the juju-created Kubernetes service to contain the right ports
        external_ports = tempo.get_external_ports(self.app.name)
        self._service_patcher = KubernetesServicePatch(self, external_ports)
        # Provide ability for Tempo to be scraped by Prometheus using prometheus_scrape
        self._scraping = MetricsEndpointProvider(
            self,
            relation_name="metrics-endpoint",
            jobs=[{"static_configs": [{"targets": [f"*:{tempo.tempo_http_server_port}"]}]}],
        )
        # Enable log forwarding for Loki and other charms that implement loki_push_api
        self._logging = LogProxyConsumer(
            self, relation_name="logging", log_files=[self.tempo.log_path], container_name="tempo"
        )
        self._grafana_dashboards = GrafanaDashboardProvider(
            self, relation_name="grafana-dashboard"
        )

        self.tracing = TracingEndpointProvider(
            self,
            host=self.hostname,
            external_url=self._external_url,
            internal_scheme="https" if self.tls_available else "http",
        )

        self.framework.observe(
            self.on["ingress"].relation_created, self._on_ingress_relation_created
        )
        self.framework.observe(
            self.on["ingress"].relation_joined, self._on_ingress_relation_joined
        )
        self.framework.observe(self.on.leader_elected, self._on_leader_elected)
        self.framework.observe(self.ingress.on.ready, self._on_ingress_ready)

        self.framework.observe(self.on.tempo_pebble_ready, self._on_tempo_pebble_ready)
        self.framework.observe(
            self.on.tempo_pebble_custom_notice, self._on_tempo_pebble_custom_notice
        )
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.tracing.on.request, self._on_tracing_request)
        self.framework.observe(self.on.tracing_relation_created, self._on_tracing_relation_created)
        self.framework.observe(self.on.tracing_relation_joined, self._on_tracing_relation_joined)
        self.framework.observe(self.on.tracing_relation_changed, self._on_tracing_relation_changed)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)
        self.framework.observe(self.on.list_receivers_action, self._on_list_receivers_action)
        self.framework.observe(self.cert_handler.on.cert_changed, self._on_cert_handler_changed)
        self.framework.observe(self.on.config_changed, self._on_config_changed)

    @property
    def _external_http_server_url(self) -> str:
        """External url of the http(s) server."""
        return f"{self._external_url}:{self.tempo.tempo_http_server_port}"

    @property
    def _external_url(self) -> str:
        """Return the external url."""
        if self.ingress.is_ready():
            ingress_url = f"{self.ingress.scheme}://{self.ingress.external_host}"
            logger.debug("This unit's ingress URL: %s", ingress_url)
            return ingress_url

        # If we do not have an ingress, then use the pod hostname.
        # The reason to prefer this over the pod name (which is the actual
        # hostname visible from the pod) or a K8s service, is that those
        # are routable virtually exclusively inside the cluster (as they rely)
        # on the cluster's DNS service, while the ip address is _sometimes_
        # routable from the outside, e.g., when deploying on MicroK8s on Linux.
        return self._internal_url

    @property
    def _internal_url(self) -> str:
        scheme = "https" if self.tls_available else "http"
        return f"{scheme}://{self.hostname}"

    @property
    def tls_available(self) -> bool:
        """Return True if tls is enabled and the necessary certs are found."""
        return (
            self.cert_handler.enabled
            and (self.cert_handler.server_cert is not None)
            and (self.cert_handler.private_key is not None)
            and (self.cert_handler.ca_cert is not None)
        )

    def _on_cert_handler_changed(self, _):
        was_ready = self.tempo.tls_ready

        if self.tls_available:
            logger.debug("enabling TLS")
            self.tempo.configure_tls(
                cert=self.cert_handler.server_cert,  # type: ignore
                key=self.cert_handler.private_key,  # type: ignore
                ca=self.cert_handler.ca_cert,  # type: ignore
            )
        else:
            logger.debug("disabling TLS")
            self.tempo.clear_tls_config()

        if was_ready != self.tempo.tls_ready:
            # tls readiness change means config change.
            self.tempo.update_config(self._requested_receivers())
            # sync scheme change with traefik and related consumers
            self._configure_ingress(_)
            self.tempo.restart()

        # sync the server cert with the charm container.
        # technically, because of charm tracing, this will be called first thing on each event
        self._update_server_cert()

        # update relations to reflect the new certificate
        self._update_tracing_v1_relations()
        self._update_tracing_v2_relations()

    def _is_legacy_v1_relation(self, relation):
        if self.tracing.is_v2(relation):
            return False

        juju_keys = {"egress-subnets", "ingress-address", "private-address"}
        # v1 relations are expected to have no data at all (excluding juju keys)
        if relation.data[relation.app] or any(
            set(relation.data[u]).difference(juju_keys) for u in relation.units
        ):
            return False

        return True

    def _configure_ingress(self, _) -> None:
        """Make sure the traefik route and tracing relation data are up to date."""
        if not self.unit.is_leader():
            return

        if self.ingress.is_ready():
            self.ingress.submit_to_traefik(
                self._ingress_config, static=self._static_ingress_config
            )
            if self.ingress.external_host:
                self._update_tracing_v1_relations()
                self._update_tracing_v2_relations()

    @property
    def legacy_v1_relations(self):
        """List of relations using the v1 legacy protocol."""
        return [r for r in self.model.relations["tracing"] if self._is_legacy_v1_relation(r)]

    def _on_tracing_request(self, e: RequestEvent):
        """Handle a remote requesting a tracing endpoint."""
        logger.debug(f"received tracing request from {e.relation.app}: {e.requested_receivers}")
        self._update_tracing_v2_relations()

    def _on_tracing_relation_created(self, e: RelationEvent):
        if not self.tracing.is_v2(e.relation):
            self._publish_v1_data(e.relation)
            # if this is the first legacy relation we get, we need to update ALL other relations
            # as we might need to add all legacy protocols to the mix
            self._update_tracing_v2_relations()

    def _on_tracing_relation_joined(self, e: RelationEvent):
        if not self.tracing.is_v2(e.relation):
            self._publish_v1_data(e.relation)

    def _on_tracing_relation_changed(self, e: RelationEvent):
        if not self.tracing.is_v2(e.relation):
            self._publish_v1_data(e.relation)

    def _on_ingress_relation_created(self, e: RelationEvent):
        self._configure_ingress(e)

    def _on_ingress_relation_joined(self, e: RelationEvent):
        self._configure_ingress(e)

    def _on_leader_elected(self, e: HookEvent):
        # as traefik_route goes through app data, we need to take lead of traefik_route if our leader dies.
        self._configure_ingress(e)

    def _on_config_changed(self, _):
        # check if certificate files haven't disappeared and recreate them if needed
        if self.tls_available and not self.tempo.tls_ready:
            logger.debug("enabling TLS")
            self.tempo.configure_tls(
                cert=self.cert_handler.server_cert,  # type: ignore
                key=self.cert_handler.private_key,  # type: ignore
                ca=self.cert_handler.ca_cert,  # type: ignore
            )

    def _update_tracing_v1_relations(self):
        for relation in self.model.relations[self.tracing._relation_name]:
            if not self.tracing.is_v2(relation):
                self._publish_v1_data(relation)

    def _publish_v1_data(self, relation: Relation):
        # we have a relation event; it might be caught by the v2 requirer
        # wrapper and turned into a `request` event, but also maybe not because
        # the remote isn't talking v2

        if not self._is_legacy_v1_relation(relation):
            logger.error(f"relation {relation} is not tracing v1. Skipping...")
            return

        logger.debug(f"updating legacy v1 relation {relation}")
        # in v1, 'receiver' was called 'ingester'.
        receivers = [
            tracing_v1.Ingester(protocol=p, port=self.tempo.receiver_ports[p])
            for p in LEGACY_RECEIVER_PROTOCOLS
        ]
        # this should be behind a leader guard
        if self.unit.is_leader():
            tracing_v1.TracingProviderAppData(host=self.hostname, ingesters=receivers).dump(
                relation.data[self.app]
            )

    def _update_tracing_v2_relations(self):
        tracing_relations = self.model.relations["tracing"]
        if not tracing_relations:
            # todo: set waiting status and configure tempo to run without receivers if possible,
            #  else perhaps postpone starting the workload at all.
            logger.warning("no tracing relations: Tempo has no receivers configured.")
            return

        requested_receivers = self._requested_receivers()
        # publish requested protocols to all v2 relations
        if self.unit.is_leader():
            self.tracing.publish_receivers(
                [(p, self.tempo.receiver_ports[p]) for p in requested_receivers]
            )

        self._restart_if_receivers_changed(requested_receivers)

    def _restart_if_receivers_changed(self, requested_receivers):
        # if the receivers have changed, we need to reconfigure tempo
        self.unit.status = MaintenanceStatus("reconfiguring Tempo...")
        updated = self.tempo.update_config(requested_receivers)
        if not updated:
            logger.debug("Config not updated; skipping tempo restart")
        if updated:
            restarted = self.tempo.restart()
            if not restarted:
                # assume that this will be handled at the next pebble-ready
                logger.debug("Cannot reconfigure/restart tempo at this time.")

    def _requested_receivers(self) -> Tuple[ReceiverProtocol, ...]:
        """List what receivers we should activate, based on the active tracing relations."""
        # we start with the sum of the requested endpoints from the v2 requirers
        requested_protocols = set(self.tracing.requested_protocols())

        # if we have any v0/v1 requirer, we'll need to activate all supported legacy endpoints
        # and publish them too (only to v1 requirers).
        if self.legacy_v1_relations:
            requested_protocols.update(LEGACY_RECEIVER_PROTOCOLS)

        # and publish only those we support
        requested_receivers = requested_protocols.intersection(set(self.tempo.receiver_ports))
        requested_receivers.update(self.tempo.enabled_receivers)
        return tuple(requested_receivers)

    def _on_tempo_pebble_custom_notice(self, event: PebbleNoticeEvent):
        if event.notice.key == self.tempo.tempo_ready_notice_key:
            logger.debug("pebble api reports ready")
            # collect-unit-status should do the rest.
            self.tempo.container.stop("tempo-ready")

    def _on_tempo_pebble_ready(self, event: WorkloadEvent):
        if not self.tempo.container.can_connect():
            logger.warning("container not ready, cannot configure; will retry soon")
            return event.defer()

        self.tempo.update_config(self._requested_receivers())
        self.tempo.plan()

        self.unit.set_workload_version(self.version)
        self.unit.status = ActiveStatus()

    def _on_update_status(self, _):
        """Update the status of the application."""
        self.unit.set_workload_version(self.version)

    def _on_ingress_ready(self, _event):
        # whenever there's a change in ingress, we need to update all tracing relations
        self._update_tracing_v1_relations()
        self._update_tracing_v2_relations()

    def _on_ingress_revoked(self, _event):
        # whenever there's a change in ingress, we need to update all tracing relations
        self._update_tracing_v1_relations()
        self._update_tracing_v2_relations()

    @property
    def version(self) -> str:
        """Reports the current Tempo version."""
        container = self.unit.get_container("tempo")
        if container.can_connect() and container.get_services("tempo"):
            try:
                return self._get_version() or ""
            # Catching Exception is not ideal, but we don't care much for the error here, and just
            # default to setting a blank version since there isn't much the admin can do!
            except Exception as e:
                logger.warning("unable to get version from API: %s", str(e))
                logger.debug(e, exc_info=True)
                return ""
        return ""

    def _get_version(self) -> Optional[str]:
        """Fetch the version from the running workload using the Tempo CLI.

        Helper function.
        """
        container = self.unit.get_container("tempo")
        proc = container.exec(["/tempo", "-version"])
        out, err = proc.wait_output()

        # example output:
        # / # /tempo --version
        # tempo, version  (branch: HEAD, revision: fd5743d5d)
        #   build user:
        #   build date:
        #   go version:       go1.18.5
        #   platform:         linux/amd64

        if version_head := re.search(r"tempo, version (.*) \(branch: (.*), revision: (.*)\)", out):
            v_head, b_head, r_head = version_head.groups()
            version = f"{v_head}:{b_head}/{r_head}"
        elif version_headless := re.search(r"tempo, version (\S+)", out):
            version = version_headless.groups()[0]
        else:
            logger.warning(
                f"unable to determine tempo workload version: output {out} "
                f"does not match any known pattern"
            )
            return
        return version

    def server_cert(self):
        """For charm tracing."""
        self._update_server_cert()
        return self.tempo.server_cert_path

    def _update_server_cert(self):
        """Server certificate for charm tracing tls, if tls is enabled."""
        server_cert = Path(self.tempo.server_cert_path)
        if self.tls_available:
            if not server_cert.exists():
                server_cert.parent.mkdir(parents=True, exist_ok=True)
                if self.cert_handler.server_cert:
                    server_cert.write_text(self.cert_handler.server_cert)
        else:  # tls unavailable: delete local cert
            server_cert.unlink(missing_ok=True)

    def tempo_otlp_http_endpoint(self) -> Optional[str]:
        """Endpoint at which the charm tracing information will be forwarded."""
        # the charm container and the tempo workload container have apparently the same
        # IP, so we can talk to tempo at localhost.
        if self.tempo.is_ready():
            return f"{self._internal_url}:{self.tempo.receiver_ports['otlp_http']}"

        return None

    def _on_collect_unit_status(self, e: CollectStatusEvent):
        if not self.tempo.container.can_connect():
            e.add_status(WaitingStatus("Tempo container not ready"))
        if not self.tempo.is_ready():
            e.add_status(WaitingStatus("Tempo API not ready just yet..."))

        e.add_status(ActiveStatus())

    @property
    def hostname(self) -> str:
        """Unit's hostname."""
        return socket.getfqdn()

    def _on_list_receivers_action(self, event: ops.ActionEvent):
        res = {}
        for receiver in self._requested_receivers():
            res[receiver.replace("_", "-")] = (
                f"{self.ingress.external_host or self.tempo.url}/{receiver}"
            )
        event.set_results(res)

    @property
    def _static_ingress_config(self) -> dict:
        entry_points = {}
        for protocol, port in self.tempo.all_ports.items():
            sanitized_protocol = protocol.replace("_", "-")
            entry_points[sanitized_protocol] = {"address": f":{port}"}

        return {"entryPoints": entry_points}

    @property
    def _ingress_config(self) -> dict:
        """Build a raw ingress configuration for Traefik."""
        http_routers = {}
        http_services = {}
        for protocol, port in self.tempo.all_ports.items():
            sanitized_protocol = protocol.replace("_", "-")
            http_routers[f"juju-{self.model.name}-{self.model.app.name}-{sanitized_protocol}"] = {
                "entryPoints": [sanitized_protocol],
                "service": f"juju-{self.model.name}-{self.model.app.name}-service-{sanitized_protocol}",
                # TODO better matcher
                "rule": "ClientIP(`0.0.0.0/0`)",
            }
            if sanitized_protocol.endswith("grpc") and not self.tls_available:
                # to send traces to unsecured GRPC endpoints, we need h2c
                # see https://doc.traefik.io/traefik/v2.0/user-guides/grpc/#with-http-h2c
                http_services[
                    f"juju-{self.model.name}-{self.model.app.name}-service-{sanitized_protocol}"
                ] = {"loadBalancer": {"servers": [{"url": f"h2c://{self.hostname}:{port}"}]}}
            else:
                # anything else, including secured GRPC, can use _internal_url
                # ref https://doc.traefik.io/traefik/v2.0/user-guides/grpc/#with-https
                http_services[
                    f"juju-{self.model.name}-{self.model.app.name}-service-{sanitized_protocol}"
                ] = {"loadBalancer": {"servers": [{"url": f"{self._internal_url}:{port}"}]}}
        return {
            "http": {
                "routers": http_routers,
                "services": http_services,
            },
        }


if __name__ == "__main__":  # pragma: nocover
    main(TempoCharm)
