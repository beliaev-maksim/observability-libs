# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""# MetricsEndpointDiscovery Library.

This library provides functionality for discovering metrics endpoints exposed
by applications deployed to a Kubernetes cluster.

It comprises:
- A custom event and event source for handling metrics endpoint changes.
- Logic to observe cluster events and emit the events as appropriate.

## Using the Library

### Handling Events

To ensure that your charm can react to changing metrics endpoint events,
use the CharmEvents extension.
```python
from charms.observability_libs.v0.metrics_endpoint_discovery import MetricsEndpointCharmEvents

class MyCharm(CharmBase):

    def __init__(self, *args):
        super().__init__(*args)

        self.metrics_endpoint_observer = MetricsEndpointObserver()

        self.framework.observe(
            self.metrics_endpoint_observer.on.metrics_endpoint_change,
            self._on_metrics_endpoint_change
        )

    def _on_metrics_endpoint_change(self, event):
        self.unit.status = ActiveStatus("metrics endpoints changed")
```
"""

import json
import logging
import os
import signal
import subprocess
import sys

from lightkube import Client
from lightkube.resources.core_v1 import Pod
from ops.charm import CharmBase, CharmEvents
from ops.framework import EventBase, EventSource, Object

logger = logging.getLogger(__name__)

# The unique Charmhub library identifier, never change it
LIBID = "a141d5620152466781ed83aafb948d03"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1

# File path where metrics endpoint change data is written for exchange
# between the discovery process and the materialised event.
PAYLOAD_FILE_PATH = "/tmp/metrics-endpoint-payload.json"


class MetricsEndpointChangeEvent(EventBase):
    """A custom event for metrics endpoint changes."""

    def __init__(self, handle):
        super().__init__(handle)

        with open(PAYLOAD_FILE_PATH, 'r') as f:
            self._discovered = json.loads(f.read())

    def snapshot(self):
        return {"payload": self._discovered}

    def restore(self, snapshot):
        self._discovered = {}

        if snapshot:
            self._discovered = snapshot["payload"]

    @property
    def discovered(self):
        return self._discovered


class MetricsEndpointChangeCharmEvents(CharmEvents):
    """A CharmEvents extension for metrics endpoint changes.

    Includes :class:`MetricsEndpointChangeEvent` in those that can be handled.
    """

    metrics_endpoint_change = EventSource(MetricsEndpointChangeEvent)


class MetricsEndpointObserver(Object):
    """Observes changing metrics endpoints in the cluster.

    Observed endpoint changes cause :class"`MetricsEndpointChangeEvent` to be emitted.
    """

    def __init__(self, charm: CharmBase, watch_names):
        super().__init__(charm, "metrics-endpoint-observer")

        self._charm = charm
        self._observer_pid = 0

        # The names of services that we are interested in for
        # determining added/removed metrics endpoints.
        self._watch_names = watch_names

    def start_observer(self):
        """Start the metrics endpoint observer running in a new process."""
        self.stop_observer()

        logging.info("Starting metrics endpoint observer process")

        # We need to trick Juju into thinking that we are not running
        # in a hook context, as Juju will disallow use of juju-run.
        new_env = os.environ.copy()
        new_env.pop("JUJU_CONTEXT_ID")

        pid = subprocess.Popen(
            [
                "/usr/bin/python3",
                "lib/charms/observability_libs/v{}/metrics_endpoint_discovery.py".format(LIBAPI),
                ",".join(self._watch_names),
                "/var/lib/juju/tools/{}/juju-run".format(self.unit_tag),
                self._charm.unit.name,
                self._charm.charm_dir,
            ],
            stdout=open("/var/log/discovery.log", "a"),
            stderr=subprocess.STDOUT,
            env=new_env,
        ).pid

        self._observer_pid = pid
        logging.info("Started metrics endopint observer process with PID {}".format(pid))

    def stop_observer(self):
        """Stop the running observer process if we have previously started it."""
        if not self._observer_pid:
            return

        try:
            os.kill(self._observer_pid, signal.SIGINT)
            msg = "Stopped running metrics endpoint observer process with PID {}"
            logging.info(msg.format(self._observer_pid))
        except OSError:
            pass

    @property
    def unit_tag(self):
        """Juju-style tag identifying the unit being run by this charm."""
        unit_num = self._charm.unit.name.split("/")[-1]
        return "unit-{}-{}".format(self._charm.app.name, unit_num)


def write_payload(payload):
    """Write the input event data to event payload file."""
    with open(PAYLOAD_FILE_PATH, "w") as f:
        f.write(json.dumps(payload))


def dispatch(run_cmd, unit, charm_dir):
    """Use the input juju-run command to dispatch a :class:`MetricsEndpointChangeEvent`."""
    dispatch_sub_cmd = "JUJU_DISPATCH_PATH=hooks/metrics_endpoint_change {}/dispatch"
    subprocess.run([run_cmd, "-u", unit, dispatch_sub_cmd.format(charm_dir)])


def main():
    """Main watch and dispatch loop.

    Watch the input k8s service names. When changes are detected, write the
    observed data to the payload file, and dispatch the change event.
    """
    watch_names, run_cmd, unit, charm_dir = sys.argv[1:]

    client = Client()
    key = "app.kubernetes.io/name"
    to_watch = watch_names.split(",")

    for change, entity in client.watch(Pod, namespace="*", labels={key: to_watch}):
        meta = entity.metadata
        payload = {
            "change": change,
            "namespace": meta.namespace,
            "name": meta.name
        }

        write_payload(payload)
        dispatch(run_cmd, unit, charm_dir)


if __name__ == "__main__":
    main()
