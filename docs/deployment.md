# Deployment

A deployment needs a Matrix homeserver whose administrator can register application services, network access between the homeserver and each bridge, and accounts or credentials for the external services. The topology inventory records intended connections; it does not configure them.

## Prepare the homeserver

Choose a stable Matrix server name and a homeserver with application-service support. Configure durable storage, access controls, monitoring and encrypted backups. Use separate homeservers or bridge instances where communities need stronger isolation. Record the selected versions and restore procedures.

A Matrix application-service registration contains two secret tokens and namespace expressions. Registration is controlled by the homeserver administrator; see the [Matrix specification](https://spec.matrix.org/latest/application-service-api/#registration). Store registrations outside version control, restrict file access, and verify that namespaces do not overlap with other services.

## Add a bridge

1. Select a bridge version and check its license, installation instructions and platform requirements. Give each instance its own configuration, credentials and database.
2. Generate and configure its application-service registration using that version's instructions. Register it with the homeserver and apply the homeserver's required restart or reload. The [mautrix Docker guide](https://docs.mau.fi/bridges/general/docker-setup.html) and [IRC bridge setup](https://matrix-org.github.io/matrix-appservice-irc/latest/bridge_setup.html) describe their respective registration steps.
3. Ensure that the bridge and homeserver can reach each other. In separate containers, `localhost` refers to the container itself. Keep bridge listeners private and secure any traffic crossing hosts.
4. Configure the external account, room and relay mode. Restrict who can control the bridge and provision connections. Obtain approval from both communities and the network operator where required.
5. Record the room and external endpoint in the inventory, then run the [integration checks](verification.md) against isolated accounts. Check every enabled direction before allowing normal traffic.

Keep passwords, access tokens, webhook URLs, registration files, encryption keys and bridge databases out of the inventory and source control. Back up bridge state and the homeserver together, and test restoration without connecting the restored copies to live services.

## QQ

Do not enable a QQ route solely because a bridge has been configured. Confirm account eligibility, current platform policy and actual incoming and outgoing delivery in a sandbox. The [architecture notes](architecture.md#bridge-candidates) identify the known uncertainty in the official bot API.
