from __future__ import annotations

import http.client
from dataclasses import dataclass
from ipaddress import ip_address

from app.qualification.verification_sdk_v1 import (
    VerificationSDKTransportFailure,
    VerificationSDKTransportResponseV1,
    VerificationSDKWireRequestV1,
)


@dataclass(frozen=True)
class VerificationSDKHTTPTransportV1:
    host: str = "127.0.0.1"
    port: int = 0
    timeout_seconds: float = 5.0

    def validate(self) -> None:
        if not _is_loopback(self.host):
            raise ValueError("Verification SDK HTTP transport may target only loopback")
        if self.port <= 0 or self.port > 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def send(
        self,
        request: VerificationSDKWireRequestV1,
    ) -> VerificationSDKTransportResponseV1:
        self.validate()
        connection = http.client.HTTPConnection(
            self.host,
            self.port,
            timeout=self.timeout_seconds,
        )
        try:
            connection.request(
                request.method,
                request.path,
                body=request.body,
                headers=dict(request.headers),
            )
            response = connection.getresponse()
            body = response.read()
            headers = {key: value for key, value in response.getheaders()}
            return VerificationSDKTransportResponseV1(
                status=response.status,
                headers=headers,
                body=body,
            )
        except (
            OSError,
            TimeoutError,
            http.client.HTTPException,
        ) as exc:
            raise VerificationSDKTransportFailure(
                f"HTTP transport failure: {exc}"
            ) from exc
        finally:
            connection.close()


def _is_loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host.strip().lower() == "localhost"
