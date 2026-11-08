# Bridge deployment scaffold

Each profile runs one bridge on an existing Docker network shared with an external Matrix homeserver. The homeserver must support application services, and its administrator must register each bridge. The scaffold publishes no ports and does not manage the homeserver or network. The IRC image is amd64-only.

Discord, Telegram and IRC use pinned images. QQ uses a small image built from an existing QQ bridge checkout through an optional overlay.

## Prepare

Choose a pre-existing Docker network reachable by the homeserver with outbound DNS and network access to the external platforms and any external databases. Set its name and choose separate private directories outside the checkout, for example:

```sh
umask 077
install -d -m 700 "$HOME/matrix-bridge-data/discord" "$HOME/matrix-bridge-data/telegram" "$HOME/matrix-bridge-data/irc"
export DISCORD_DATA_DIR="$HOME/matrix-bridge-data/discord"
export TELEGRAM_DATA_DIR="$HOME/matrix-bridge-data/telegram"
export IRC_DATA_DIR="$HOME/matrix-bridge-data/irc"
export MATRIX_NETWORK='your-existing-network'
```

Set only the directories for bridges you operate. The fallback paths under `/var/empty/matrix-relay-hub` are placeholders; leave them absent and set the appropriate variable before starting a profile. Compose will refuse a missing bind source rather than create it. Keep data directories private and back them up securely; they contain credentials, registration tokens and persistent bridge state. With rootful Docker, Discord's initialization can create root-owned files; on later startup the image changes `/data` ownership to UID 1337. Plan a private administrator-controlled editing and backup method for your Docker UID mapping. Avoid copying credentials into an unprotected location.

## Configure and register

Use the version-specific configuration references and setup guides:

- Discord v0.7.7: [sample configuration](https://github.com/mautrix/discord/blob/v0.7.7/example-config.yaml), [authentication](https://docs.mau.fi/bridges/go/discord/authentication.html).
- Telegram v26.10: [sample configuration](https://docs.mau.fi/configs/mautrix-telegram/v26.10.html), [authentication](https://docs.mau.fi/bridges/go/telegram/authentication.html).
- IRC 4.0.1: [sample configuration](https://github.com/matrix-org/matrix-appservice-irc/blob/4.0.1/config.sample.yaml), [bridge setup](https://github.com/matrix-org/matrix-appservice-irc/blob/4.0.1/docs/bridge_setup.md), [image startup script](https://github.com/matrix-org/matrix-appservice-irc/blob/4.0.1/docker/start.sh). The image expects `/data/config.yaml` and `/data/appservice-registration-irc.yaml`.
- QQ: an existing bridge checkout containing `src/qq_bridge/`, configured below.

For Discord and Telegram, follow the [mautrix Docker setup](https://docs.mau.fi/bridges/general/docker-setup.html) for initialization and registration generation. Initial startup may create config files and generate tokens. Replace the generated `example.com` domain and set the homeserver address before the next run; the images exit 0 even when registration generation fails for missing settings, so check that `registration.yaml` exists before registering the bridge. The profiles do not automatically restart; configure a restart policy only after initialization and registration are complete. Review the version's config before connecting to real rooms; do not print or commit configuration, tokens or registration files. IRC requires a configured registration file as described in its setup guide. Follow the relevant guide for credentials, storage, permissions and relay settings. Put file-based databases, keys and other persistent files under `/data`, or use a separately managed persistent database dedicated to that bridge.

### IRC registration

The IRC image needs `/data/config.yaml` and `/data/appservice-registration-irc.yaml` before it starts. Copy the tag's sample config into the data directory, set `homeserver.url`, `homeserver.domain` and the server under `ircService.servers`, then generate the registration without network access:

```sh
curl -fsS https://raw.githubusercontent.com/matrix-org/matrix-appservice-irc/4.0.1/config.sample.yaml -o "$IRC_DATA_DIR/config.yaml"
# edit config.yaml
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$IRC_DATA_DIR:/data" \
  --entrypoint node matrixdotorg/matrix-appservice-irc@sha256:c571a02af827c5bc623a5ad3551d6107318a241b182f80bacbee4a0a7b3df1f7 \
  app.js -r -f /data/appservice-registration-irc.yaml \
  -u http://matrix-relay-irc:8090 -c /data/config.yaml
```

`-u` is the address the homeserver uses to reach the bridge. The bot localpart defaults to `appservice-irc`; `-l <localpart>` changes it.

Configure the homeserver URL as reachable from the bridge, and the appservice URL as reachable from the homeserver over the shared network. `localhost` inside a container refers to that container. Use the configured internal port and a unique network alias for each appservice URL. Defaults are `matrix-relay-discord`, `matrix-relay-telegram`, `matrix-relay-irc` and `matrix-relay-qq`; set `DISCORD_NETWORK_ALIAS`, `TELEGRAM_NETWORK_ALIAS`, `IRC_NETWORK_ALIAS` or `QQ_NETWORK_ALIAS` to change them. Docker does not enforce alias uniqueness, so check the shared network for collisions. Ensure appservice IDs and namespaces do not collide. Give the registration file to the homeserver administrator through a secure channel and follow [appservice registration](https://docs.mau.fi/bridges/general/registering-appservices.html). Synapse needs access to the file inside its container and a restart after registration changes. Follow your homeserver's own restart or reload procedure; update both sides when registration-related settings change.

### QQ (official bot)

Build the QQ bridge image from its own checkout. The checkout must contain `src/qq_bridge/`:

```sh
export QQ_BRIDGE_SOURCE="$HOME/matrix-qq-bridge"   # your checkout
docker build -f deploy/qq/Dockerfile -t matrix-relay-hub/qq-bridge:local "$QQ_BRIDGE_SOURCE"
```

Create its data directory and set the image plus the host user that owns the directory:

```sh
install -d -m 700 "$HOME/matrix-bridge-data/qq"
export QQ_DATA_DIR="$HOME/matrix-bridge-data/qq"
export QQ_BRIDGE_IMAGE=matrix-relay-hub/qq-bridge:local
export QQ_UID="$(id -u)" QQ_GID="$(id -g)"
```

Write `config.toml` and `registration.yaml` there. The bridge reads `/data/config.toml`; the homeserver needs the registration. Point the bridge at the homeserver over the shared network and make its listener reachable by its alias:

```toml
[matrix]
homeserver = "http://your-homeserver:8008"
domain = "example.org"
as_token = "..."
hs_token = "..."
bot_mxid = "@qqbot:example.org"
listen_host = "0.0.0.0"
listen_port = 29335

[qq]
app_id = "..."
app_secret = "..."

[[bridge]]
room = "#welcome:example.org"
code = "welcome"
```

Copy the bridge's `deploy/registration.example.yaml`, replace the tokens and domain, and set `url: http://matrix-relay-qq:29335`. Set `QQ_NETWORK_ALIAS` if that name is taken. Start the overlay together with the base file:

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.qq.yaml --profile qq up -d
```

The bot must be added to the QQ group with message receiving and active posting enabled. [Tencent's send documentation](https://raw.githubusercontent.com/tencent-connect/bot-docs/main/docs/develop/api-v2/server-inter/message/send-receive/send.md) says proactive push ended on 21 April 2025, so Matrix-to-QQ delivery depends on current account permissions and may be rejected; test in a sandbox group first. The bridge does not support encrypted Matrix rooms and replaces media over 3.5 MB with a notice. Check its transaction handling before use: older revisions consumed a Matrix transaction before parsing its body, so a malformed delivery dropped the retry; the [migration checklist](../docs/migration.md) describes the check.

## Operate

From the repository root, after configuration and registration, select any combination of profiles:

```sh
docker compose -f deploy/compose.yaml --profile discord up -d
docker compose -f deploy/compose.yaml --profile telegram --profile irc up -d
docker compose -f deploy/compose.yaml -f deploy/compose.qq.yaml --profile qq up -d
```

Each command affects the selected services; include all desired profiles when reconciling the full set. After starting a bridge, check its state, then verify registration, authentication and message delivery using the [integration checks](../docs/verification.md). Compose process status alone does not establish bridge readiness. For example:

```sh
docker compose -f deploy/compose.yaml --profile discord ps --all
```

Inspect service logs privately when diagnosing failures; they may contain credentials or message content. To apply changed Compose settings or an image digest, use `up -d` again; `restart` alone does not recreate containers. Restart the relevant bridge after changes to its configuration. Keep one instance per bridge and state store, and never run the old and replacement instance concurrently. For a cutover, stop and verify the old instance using its original host and orchestrator, securely back up its state, update registration if required, then start the replacement. The following command stops only the Discord service in this Compose project:

```sh
docker compose -f deploy/compose.yaml --profile discord stop discord
```

Stopping preserves the data and homeserver registration; remove or disable the registration through the homeserver's own procedure if retiring the bridge. Avoid `down -v` or manual deletion without a verified backup.

## Offline Compose verification

These commands only render and validate Compose configuration; they do not require the directories or network to exist and do not pull images or start containers. Run from the repository root:

```sh
(
  export MATRIX_NETWORK=offline-test
  export DISCORD_DATA_DIR=/tmp/matrix-bridge-test/discord
  export TELEGRAM_DATA_DIR=/tmp/matrix-bridge-test/telegram
  export IRC_DATA_DIR=/tmp/matrix-bridge-test/irc
  export QQ_BRIDGE_IMAGE=matrix-relay-hub/qq-bridge:offline-test
  export QQ_DATA_DIR=/tmp/matrix-bridge-test/qq
  export QQ_UID=1000
  export QQ_GID=1000
  for profile in discord telegram irc; do
    docker compose -f deploy/compose.yaml --profile "$profile" config --quiet || exit
  done
  docker compose -f deploy/compose.yaml --profile discord --profile telegram --profile irc config --quiet && \
    docker compose -f deploy/compose.yaml -f deploy/compose.qq.yaml --profile qq config --quiet
)
```

The offline configuration tests run with `python3 -m unittest discover -s deploy -p 'test_compose*.py' -v`. They skip when Docker Compose is unavailable.

## Security and limits

A shared external network is not an isolation boundary: other network members can reach listening services. Restrict network membership and access, safeguard bridge credentials, and review encryption, retention, room membership and access controls. Obtain explicit consent from participants before relaying messages and review each bridge's relay mode and platform terms. Telegram v26.10 defaults to relay disabled and can kick Matrix users who lack a remote login when no relay is set; review these settings before connecting rooms. Obtain IRC network operator permission and check connection limits: the bridge may create an IRC client per Matrix user.

These profiles and actual platform interactions have not been tested in production. The QQ image is built from a working tree, so record the source commit you built. Other bridges can be supplied in operator-authored Compose files attached to the same existing network; assess their images, permissions, storage and compatibility separately.
