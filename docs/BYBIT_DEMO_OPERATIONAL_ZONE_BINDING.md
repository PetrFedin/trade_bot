# Bybit Demo operational zone binding

This layer prevents individually valid manual source runs from being assembled into one operational release chain when the protected resources changed between dispatches.

The problem is different from Git-SHA provenance and same-account validation:

- exact-head provenance proves every source run executed the same code;
- same-account validation proves the Demo read-only and trading credentials belong to one account inside a credential-bearing run;
- operational-zone binding proves the protected database and Demo account stayed the same across the separate manual runs used by one release chain.

## Protected binding secret

The `bybit-demo` GitHub Environment requires one additional secret:

```text
BYBIT_DEMO_ZONE_BINDING_SECRET
```

Requirements:

- at least 32 characters;
- generated independently from PostgreSQL and Bybit credentials;
- available only to the protected source workflows that create zone sidecars;
- never passed to the GitHub-hosted release-evidence assembler;
- never serialized into an artifact.

Rotating this secret between source runs intentionally invalidates continuity. A new release-evidence chain must then be collected from the beginning under the new binding secret.

## Database identity

The database sidecar does **not** hash the raw DSN.

It canonicalizes only the resource identity needed to distinguish one operational PostgreSQL target from another:

```text
host
hostaddr when configured
port
database name
sslmode
target_session_attrs
```

Username and password are excluded. Therefore normal credential rotation on the same database does not break continuity, while changing the host, port, database or relevant connection target semantics does.

The canonical resource identity is HMAC-SHA256 bound under `BYBIT_DEMO_ZONE_BINDING_SECRET`. The host/database values themselves are not exposed in the sidecar.

## Demo account identity

Where a protected workflow already has the Demo read-only credential, the sidecar performs the existing authenticated GET-only account-identity inspection and binds:

```text
userID
parentUid
isMaster
```

The raw identifiers are not serialized. API-key rotation inside the same authenticated Demo account does not change the account binding. Changing to another Demo account does.

No extra Bybit credentials are granted to DB-only workflows just to obtain this proof.

## Resource matrix

The release chain requires the following sidecar resources:

| Stage | Database binding | Demo account binding |
|---|---:|---:|
| activation readiness | required | required |
| session start/status | required | not supplied |
| persistent supervisor | required | required |
| v121 ARM | required | required |
| operator-approved entry | required | required |
| v121 HALT | required | required |
| v123 recovery receipt | required | not supplied |

Session and recovery remain DB-only. Recovery therefore continues to receive no Bybit API credential.

## Sidecar artifact

Each successful protected producer run creates:

```text
artifacts/bybit-demo-operational-zone-binding.json
```

with schema:

```text
BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V1
```

The sidecar contains only:

- producer identity;
- exact Git SHA;
- observation timestamp;
- HMAC algorithm marker;
- HMAC binding-key marker;
- opaque database binding token;
- optional opaque Demo-account binding token;
- explicit no-order-write/no-mainnet capability flags.

It does not contain the DSN, database host/name, DB username/password, API key, API secret, userID, parentUid, balances, positions or order data.

The sidecar is uploaded in the **same GitHub Actions artifact** as the producer evidence so the release assembler obtains both from the same exact run ID.

## Release validation

The GitHub-hosted `bybit-demo-operational-release-evidence` workflow still receives no protected secrets. It downloads the sidecars already produced by the protected runs and validates:

1. a sidecar exists for every supplied release stage;
2. sidecar producer identity matches the stage;
3. sidecar Git SHA matches the release Git SHA;
4. sidecar `observed_at` falls inside that exact source-run window;
5. all sidecars use the same binding-key marker;
6. every supplied stage has the same operational database token;
7. every credential-bearing stage has the same Demo-account token;
8. DB-only stages do not unexpectedly acquire account binding;
9. all capability flags remain non-trading and non-mainnet.

Any mismatch returns `BLOCKED` with bounded reason codes such as:

```text
OPERATIONAL_ZONE_BINDING_SECRET_DRIFT
OPERATIONAL_ZONE_DATABASE_DRIFT
OPERATIONAL_ZONE_DEMO_ACCOUNT_DRIFT
OPERATIONAL_ZONE_BINDING_OUTSIDE_SOURCE_RUN:<stage>
```

## Final manifest

The final release artifact does **not** copy the stable resource HMAC tokens.

Instead it records:

```text
operational_zone_binding_verified = true|false
zone_binding_sha256 = { stage -> exact sidecar file SHA-256 }
```

Those sidecar hashes are themselves covered by the final canonical `manifest_sha256`. This makes the final release evidence reproducible without publishing stable resource pseudonyms in the consolidated artifact.

## Password and API-key rotation

Expected behavior:

- PostgreSQL username/password rotation against the same resource: continuity preserved;
- Demo API-key rotation inside the same authenticated Demo account: continuity preserved;
- database host/database change: continuity blocked;
- Demo account change: continuity blocked;
- zone-binding secret rotation during a chain: continuity blocked.

After an intentional resource migration or binding-secret rotation, collect a completely new source-run chain beginning with activation readiness.

## Safety boundary

Zone binding does not:

- ARM or HALT v121;
- create an operator approval;
- create or manage an exchange order;
- modify PostgreSQL;
- change strategy, ranking, sizing or risk rules;
- create a fallback entry path;
- add any mainnet routing capability.

The sidecar client uses only the existing GET-only Demo account identity surface when account binding is required.

Pull-request qualification never dispatches protected workflows and never creates real Demo orders. Real source sidecars appear only when an operator deliberately runs the existing protected workflow sequence.
