# Matrix application-service lab

This runs Synapse v1.145.0 and two independent synthetic application services on an internal Docker network. It uses disposable local accounts and secrets, and publishes no host ports. No existing bridges or external services are involved.

## Run

Requirements: Docker with a working daemon, Python 3.12, and enough disk space for the Synapse image (about 110 MB compressed). From the repository root:

```sh
docker pull --platform linux/amd64 matrixdotorg/synapse@sha256:b1977204551e49b7e086eb7776ab70fea0be524ecc3753f2f169d687219995b7
python3.12 lab/run.py
```

The runner uses only the locally cached digest and makes no image builds or downloads. Each service receives its own read-only token file and script; Synapse's data and the other service's tokens are not mounted into it. The runner checks those mounts, applies memory and process limits and command timeouts, and attempts cleanup on exit, including ordinary failures and SIGINT/SIGTERM. It leaves the downloaded image in Docker's cache.

Cleanup selects containers and networks using the unique run label and exact expected names. A Docker timeout, signal, or failed mutating command may leave a daemon request in progress; the runner reports cleanup as uncertain and prints the specific run label for inspection. A Docker daemon failure, SIGKILL, or host crash can prevent cleanup or reporting. Inspect the affected run's labeled resources and `lab/.run-*` directory before removing only those resources. Docker output is suppressed because it can contain ephemeral secrets.

## What it checks

A normal local user creates a non-federated room and invites one registered ghost from each application service. The normal user and both ghosts each send a message. The test waits for each service to receive three distinct event IDs, then checks the sender, room, type and body. A second room containing only the normal user and ghost A checks that Synapse does not dispatch its events to B. Each service's test-only event reader requires its own token; the test checks that the other service's token is rejected in both directions. After delivery, the test observes five seconds and checks both services for unexpected shared-room messages. These set-based checks do not detect repeated delivery of the same event ID. Failures return a nonzero status.

The homeserver exposes only the client API, has no trusted key servers, disables federation destinations and reporting, and runs on an internal Docker network. Internal Docker networks do not isolate containers from the host or necessarily prevent access to the host gateway. The test services keep received events in memory and do not relay or generate messages; their HTTP endpoints are intended only for this disposable lab. Containers share a network, and tokens travel over plain HTTP; the dispatch and token checks are not a general security proof. The bounded echo check does not establish loop safety for real bridges, other message types, retries, or longer periods. This tests Matrix event composition only; it does not test Discord, Telegram, IRC, QQ, or any actual bridge.

## References

- [Synapse Docker image](https://hub.docker.com/r/matrixdotorg/synapse) — upstream image distribution and usage.
- [Docker network create](https://docs.docker.com/reference/cli/docker/network/create/) — `--internal` network behavior.
