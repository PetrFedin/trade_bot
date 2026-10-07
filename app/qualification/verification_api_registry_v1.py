from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from app.qualification.persistent_trust_state_authority_v1 import (
    PersistentTrustStateAuthorityV1,
)
from app.qualification.portable_artifact_codec import canonical_json_bytes


class VerificationAPIRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class VerificationAuthorityRegistryV1:
    authorities: Mapping[str, Path]

    def __post_init__(self) -> None:
        normalized: dict[str, Path] = {}
        for authority_id, directory in self.authorities.items():
            key = authority_id.strip()
            if not key:
                raise VerificationAPIRegistryError("authority_id cannot be blank")
            if key in normalized:
                raise VerificationAPIRegistryError(
                    f"duplicate authority_id: {key}"
                )
            path = Path(directory)
            if not path.is_absolute():
                raise VerificationAPIRegistryError(
                    f"authority path must be absolute for {key}"
                )
            normalized[key] = path
        object.__setattr__(self, "authorities", MappingProxyType(normalized))

    def resolve(self, authority_id: str) -> PersistentTrustStateAuthorityV1:
        key = authority_id.strip()
        if not key:
            raise VerificationAPIRegistryError("authority_id cannot be blank")
        try:
            directory = self.authorities[key]
        except KeyError as exc:
            raise VerificationAPIRegistryError(
                f"unknown authority_id: {key}"
            ) from exc
        return PersistentTrustStateAuthorityV1(directory)


@dataclass(frozen=True)
class VerificationTrustedRootRegistryV1:
    root_sets: Mapping[str, Mapping[str, bytes]]

    def __post_init__(self) -> None:
        normalized_sets: dict[str, Mapping[str, bytes]] = {}
        for root_set_id, roots in self.root_sets.items():
            set_id = root_set_id.strip()
            if not set_id:
                raise VerificationAPIRegistryError(
                    "trusted_root_set_id cannot be blank"
                )
            if set_id in normalized_sets:
                raise VerificationAPIRegistryError(
                    f"duplicate trusted_root_set_id: {set_id}"
                )
            if not roots:
                raise VerificationAPIRegistryError(
                    f"trusted root set cannot be empty: {set_id}"
                )
            normalized_roots: dict[str, bytes] = {}
            for key_id, public_key in roots.items():
                normalized_key_id = key_id.strip()
                if not normalized_key_id:
                    raise VerificationAPIRegistryError(
                        f"blank trusted root key_id in {set_id}"
                    )
                if normalized_key_id in normalized_roots:
                    raise VerificationAPIRegistryError(
                        f"duplicate trusted root key_id in {set_id}: "
                        f"{normalized_key_id}"
                    )
                key_bytes = bytes(public_key)
                if len(key_bytes) != 32:
                    raise VerificationAPIRegistryError(
                        f"trusted root {normalized_key_id} in {set_id} "
                        "must be 32 bytes"
                    )
                normalized_roots[normalized_key_id] = key_bytes
            normalized_sets[set_id] = MappingProxyType(normalized_roots)
        object.__setattr__(
            self,
            "root_sets",
            MappingProxyType(normalized_sets),
        )

    def resolve(self, trusted_root_set_id: str) -> Mapping[str, bytes]:
        return self.resolve_with_digest(trusted_root_set_id)[0]

    def resolve_with_digest(
        self,
        trusted_root_set_id: str,
    ) -> tuple[Mapping[str, bytes], str]:
        set_id = trusted_root_set_id.strip()
        if not set_id:
            raise VerificationAPIRegistryError(
                "trusted_root_set_id cannot be blank"
            )
        try:
            roots = self.root_sets[set_id]
        except KeyError as exc:
            raise VerificationAPIRegistryError(
                f"unknown trusted_root_set_id: {set_id}"
            ) from exc
        payload = {
            "trusted_root_set_id": set_id,
            "roots": [
                {
                    "key_id": key_id,
                    "public_key_b64": base64.b64encode(
                        roots[key_id]
                    ).decode("ascii"),
                }
                for key_id in sorted(roots)
            ],
        }
        digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        return roots, digest
