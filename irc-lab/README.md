# Matrix–IRC forwarding proof

This local check starts Synapse 1.145.0, matrix-appservice-irc 4.0.1, and Ergo 2.19.1 on a fresh internal Docker network. It creates a disposable Matrix user and room, joins an IRC client to `#lab`, and checks one message in each direction, including sender, content and destination. It does not contact a real Matrix or IRC network.

## Run

Requires Docker Engine, Python 3, network access to the container registries for the first two pulls, and the pinned Synapse image already cached. On Linux amd64, the bridge and Ergo image manifests list 123,713,391 and 18,277,963 compressed layer bytes respectively (141,991,354 bytes total). Docker may reuse cached layers.

```sh
docker image inspect matrixdotorg/synapse@sha256:b1977204551e49b7e086eb7776ab70fea0be524ecc3753f2f169d687219995b7 >/dev/null && \
docker pull matrixdotorg/matrix-appservice-irc@sha256:c571a02af827c5bc623a5ad3551d6107318a241b182f80bacbee4a0a7b3df1f7 && \
docker pull ghcr.io/ergochat/ergo@sha256:ef885e44f7fa19101bbbc41baef11dc280dc8107465dccaf6f0860f41b48a682 && \
python3 irc-lab/run.py
```

The first command must succeed before pulling. The test uses no published ports, Docker socket mount, host networking or privileged containers. Services have memory and process limits. Docker's [internal network](https://docs.docker.com/reference/cli/docker/network/create/#network-internal-mode---internal) blocks external routing, but containers may still reach host services via the bridge gateway; it is not a complete host isolation boundary. Synapse uses a client-only listener, with federation destinations, trusted key servers, URL previews and statistics reporting disabled. Proxy environment variables are cleared in every container.

Temporary configuration and random credentials live under `irc-lab/` during a run. Cleanup checks each exact role name and run label before removal. The run label is printed at startup; if a command fails, times out or is interrupted, cleanup reports uncertainty even after reconciliation. Inspect that label and the exact resource names before removing any leftovers. An uncatchable termination or Docker failure can prevent cleanup. Image layers remain in Docker's shared cache.

## Scope

This is a short-lived single-host smoke test with a static room/channel mapping and unencrypted traffic inside the isolated network. It does not test federation, TLS, persistence, real network policy, other platforms, reliability under load, or production operation. The local Synapse permits registration without verification for the duration of the test.

## Images and references

- [Synapse 1.145.0](https://github.com/element-hq/synapse/tree/v1.145.0), AGPL-3.0-or-later or Element commercial license, per the pinned image metadata. [Synapse Docker setup](https://github.com/element-hq/synapse/blob/v1.145.0/docker/README.md).
- [matrix-appservice-irc 4.0.1](https://github.com/matrix-org/matrix-appservice-irc/tree/4.0.1), [Apache-2.0](https://github.com/matrix-org/matrix-appservice-irc/blob/4.0.1/LICENSE). See its [bridge setup](https://matrix-appservice-irc.readthedocs.io/en/latest/bridge_setup/) and [versioned configuration](https://github.com/matrix-org/matrix-appservice-irc/blob/4.0.1/config.sample.yaml).
- [Ergo v2.19.1](https://github.com/ergochat/ergo/releases/tag/v2.19.1), [MIT](https://github.com/ergochat/ergo/blob/v2.19.1/LICENSE). Its [Docker instructions](https://github.com/ergochat/ergo/blob/v2.19.1/distrib/docker/README.md) identify GHCR as the official registry. The pinned image index resolves to linux/amd64 manifest `sha256:d96402341db6edd6302bde1a8a418a90abc7bfc99ee08599ee477fc51cb1ce71`.
- [Matrix Application Service API](https://spec.matrix.org/latest/application-service-api/).
