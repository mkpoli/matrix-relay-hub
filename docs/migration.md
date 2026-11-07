# Migration

Move one community at a time. A migration changes message delivery and can expose room history to new participants; obtain approval from the administrators and participants of each connected community first.

## Inventory

Record the current homeserver, rooms, external endpoint IDs, bridge versions, relay accounts, application-service registrations, namespaces, capabilities and moderation settings. Note the owner and storage location of each credential without copying secrets into the topology inventory. Record the homeserver database, bridge databases and encryption keys needed for a coordinated restore. Confirm the existing topology and actual running configuration rather than relying on documentation alone.

Document which events each route carries and its known delivery limits. For QQ, verify the account's eligibility, incoming event coverage and outbound permissions against current platform policy before planning a cutover. Decide whether history should remain in place; the inventory does not provide backfill.

## Rehearsal

1. Restore a coordinated backup into an isolated test environment without production bridge credentials or network access to production endpoints.
2. Configure test rooms and test accounts. Check every directed platform pair, including attribution, loops, duplicate deliveries, media, replies, edits and redactions.
3. Test bridge and homeserver restarts, duplicate transactions, interrupted sends, rate limits, credential revocation and recovery from backup. Record unsupported features and unknown delivery outcomes.
4. Verify access controls and the visibility of room history on each network. Confirm the rollback operator and acceptance criteria.

## Cutover

1. Schedule a maintenance window and notify participants. Back up and verify the current homeserver and bridge state.
2. Pause the old routes and incoming processing where supported. Drain queues and record cursors and messages with uncertain outcomes. Some external platforms cannot pause or replay ingress; account for the resulting gap explicitly.
3. Transfer or restore bridge state, registrations and credentials using the bridge's documented procedure. Preserve ghost namespaces and message mappings where supported; independently created identities and databases are not interchangeable.
4. Enable one writer for each route. Run a synthetic message in each direction, check for loops and duplicate echoes, then monitor delivery and failures.
5. If rollback is needed, pause the new routes, reconcile in-flight and uncertain messages, and reactivate the old configuration as a controlled second cutover. Do not leave both writers active.

No live infrastructure is modified by the inventory checker. Bridge-specific state migration and delivery recovery require procedures tested against the chosen versions.
