# Bybit Demo operational zone binding

This layer prevents individually valid manual source runs from being assembled into one operational release chain when protected resources changed between dispatches.

It is intentionally separate from Git-SHA provenance and same-account validation:

- exact-head provenance proves every source run executed the same code;
- same-account validation proves the Demo read-only and trading credentials belong to one account inside a credential-bearing run;
- operational-zone binding proves the protected database lineage and Demo account stayed the same across the separate manual runs used by one release chain.

## Protected binding secret

The `bybit-demo` GitHub Environment requires:

```text
BYBIT_DEMO_ZONE_BINDING_SECRET
```

Requirements:

- at least 32 characters;
- generated independently from PostgreSQL and Bybit credentials;
- available only to protected source workflows that create zone sidecars;
- never passed to the GitHub-hosted release-evidence assembler;
- never serialized into an artifact.

Rotating this secret between source runs intentionally invalidates continuity. Collect a new release chain from activation readiness after rotation.

## Database identity v2

The database sidecar does **not** hash the raw DSN and does not expose the v124 UUID.

Its HMAC input combines two layers.

### Endpoint/resource semantics

```text
host
hostaddr when configured
port
database name
sslmode
target_session_attrs
```

Username and password are excluded. Therefore normal database credential rotation does not change the binding.

### Immutable logical lineage

Every protected sidecar also reads:

```text
astra_bybit_demo_operational_identity_v124
```

through the read-only logical-database identity reader and includes its immutable UUID inside the HMAC input.

The raw UUID is never serialized. The sidecar only reports:

```text
logical_database_identity_verified = true
```

This closes the case where the same DNS/port/database name starts pointing at a separately initialized operational database.

Expected behavior:

- same endpoint + same v124 UUID: same database binding;
- password/user rotation: same database binding;
- same endpoint + independently bootstrapped database UUID: different binding;
- different endpoint/database: different binding;
- backup/restore continuing the same logical database: v124 UUID preserved.

See `BYBIT_DEMO_LOGICAL_DATABASE_IDENTITY_V124.md` for the underlying singleton contract.

## Demo account identity

Where a protected workflow already has the Demo read-only credential, the sidecar performs authenticated GET-only account-identity inspection and binds:

```text
userID
parentUid
isMaster
```

The raw identifiers are not serialized. API-key rotation inside the same authenticated Demo account does not change the account binding. Changing to another Demo account does.

No extra Bybit credentials are granted to DB-only workflows just to obtain account proof.

## Resource matrix

| Stage | v124-backed database binding | Demo account binding |
|---|---:|---:|
| activation readiness | required | required |
| session start/status | required | not supplied |
| persistent supervisor | required | required |
| v121 ARM | required | required |
| operator-approved entry | required | required |
| v121 HALT | required | required |
| v123 recovery receipt | required | not supplied |

Session and recovery remain DB-only. Recovery continues to receive no Bybit API credential.

## Sidecar artifact

Each successful protected producer run creates:

```text
artifacts/bybit-demo-operational-zone-binding.json
```

with schema:

```text
BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V2
```

The sidecar contains only:

- producer identity;
- exact Git SHA;
- observation timestamp;
- HMAC algorithm marker;
- HMAC binding-key marker;
- opaque v124-backed database binding token;
- `logical_database_identity_verified` boolean;
- optional opaque Demo-account binding token;
- explicit no-order-write/no-mainnet capability flags.

It does not contain the DSN, database host/name, database UUID, DB username/password, API key, API secret, userID, parentUid, balances, positions or order data.

The sidecar is uploaded in the **same GitHub Actions artifact** as producer evidence, so release assembly obtains both from the same exact run ID.

## Release validation

The GitHub-hosted `bybit-demo-operational-release-evidence` workflow receives no protected secrets. It downloads sidecars already produced by protected runs and requires:

1. sidecar for every supplied stage;
2. schema exactly `BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V2`;
3. `logical_database_identity_verified=true` for every stage;
4. exact producer identity and Git SHA;
5. `observed_at` inside the exact source-run window;
6. one binding-key marker across the chain;
7. one v124-backed operational database token across all supplied stages;
8. one Demo-account token across credential-bearing stages;
9. no unexpected account token on DB-only stages;
10. all capability flags non-trading and non-mainnet.

Only after the v124 checks succeed does the assembler delegate to the already-qualified V1 same-zone continuity contract.

Legacy V1 sidecars cannot complete a v124 release chain.

Bounded failures include:

```text
OPERATIONAL_ZONE_V124_SCHEMA_INVALID:<stage>
OPERATIONAL_ZONE_LOGICAL_DB_IDENTITY_MISSING:<stage>
OPERATIONAL_ZONE_BINDING_SECRET_DRIFT
OPERATIONAL_ZONE_DATABASE_DRIFT
OPERATIONAL_ZONE_DEMO_ACCOUNT_DRIFT
OPERATIONAL_ZONE_BINDING_OUTSIDE_SOURCE_RUN:<stage>
```

## Final manifest

The final release artifact does **not** copy stable database/account HMAC tokens.

Instead it records:

```text
operational_zone_binding_verified = true|false
zone_binding_sha256 = { stage -> exact sidecar file SHA-256 }
```

Those sidecar hashes are covered by the final canonical `manifest_sha256`. This keeps consolidated evidence reproducible without publishing stable resource pseudonyms or the logical database UUID.

## Rotation and migration rules

- PostgreSQL username/password rotation on the same logical DB: continuity preserved.
- Demo API-key rotation inside the same authenticated Demo account: continuity preserved.
- Normal restore/failover that preserves the v124 logical DB identity: continuity preserved.
- Independently bootstrapped DB at the same endpoint: continuity blocked.
- Database host/database change: continuity blocked.
- Demo account change: continuity blocked.
- Zone-binding secret rotation during a chain: continuity blocked.

After intentional logical-database replacement or binding-secret rotation, collect a completely new source-run chain beginning with activation readiness.

## Safety boundary

Zone binding does not:

- ARM or HALT v121;
- create operator approval;
- create or manage an exchange order;
- change strategy, ranking, sizing or risk rules;
- create fallback entry behavior;
- add mainnet routing;
- require `pg_monitor` or another PostgreSQL monitoring role.

The only database mutation introduced by this feature is the separately explicit v124 bootstrap migration that creates the immutable singleton. Normal sidecar generation is read-only against PostgreSQL plus GET-only against Demo account identity when account binding is required.

Pull-request qualification never dispatches protected workflows and never creates real Demo orders.
