# Matrix Relay Hub

Matrix Relay Hub is a standalone inventory for a shared, Matrix-centered bridge deployment. External networks connect through separately operated Matrix application services. The inventory checker detects conflicting identifiers and endpoint assignments before an operator configures those services.

**Status:** The checker does not run bridges, provision rooms, forward messages or verify connectivity. Cross-platform forwarding and migration require the existing bridge software and integration tests.

## Check an inventory

Python 3.12 or newer is required. From the project directory:

```sh
PYTHONPATH=src python3 -m relay_hub check examples/topology.toml
```

Use the [example inventory](examples/topology.toml) as a starting point. Supply trusted, finite local files; the checker reads each file into memory. Each plugin entry represents one independently configured bridge instance; each community contains a Matrix room ID and its external endpoints. `kind` is a descriptive string, and `namespace` is an operator-supplied identifier. Configuration references are opaque strings and are never opened. Keep credentials in the bridge's own protected configuration, not in the inventory.

The checker validates the schema, checks room-ID shape and detects duplicate community IDs, room IDs, plugin IDs and namespace strings. It accepts domainless IDs introduced in [Matrix room version 12](https://spec.matrix.org/v1.19/rooms/v12/). It checks endpoint references and rejects repeated remote IDs within a plugin instance. It cannot detect overlapping Matrix namespace regular expressions, equivalent remote IDs across separate plugin instances, ownership, access controls or loops. It is not a security boundary.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

- [Architecture and bridge candidates](docs/architecture.md)
- [Deployment](docs/deployment.md)
- [Matrix application-service lab](lab/README.md)
- [Integration verification](docs/verification.md)
- [Migration checklist](docs/migration.md)

The project is available under the [MIT License](LICENSE). Independent bridge software is distributed under its own license.
