# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Authenticode signing plan shared with scripts/sign_release.ps1.

This module does not call signtool. It decides which backend would run and
which arguments that backend would receive. The Windows script is the process
that actually signs a binary when a certificate is present.
"""

from __future__ import annotations

DEFAULT_TIMESTAMP_URL = "https://timestamp.acs.microsoft.com"
FALLBACK_TIMESTAMP_URL = "https://timestamp.digicert.com"


class SigningPlanError(ValueError):
    """The requested signing options cannot be turned into a command."""


def plan_authenticode(
    *,
    file_path: str,
    azure_endpoint: str = "",
    azure_account: str = "",
    azure_profile: str = "",
    dlib_path: str = "",
    csp_name: str = "",
    key_container: str = "",
    cert_file: str = "",
    pfx_path: str = "",
    thumbprint: str = "",
    self_signed: bool = False,
    require_trusted: bool = False,
    timestamp_url: str = "",
    timestamp_fallback: str = "",
) -> dict[str, object]:
    """Return the signing backend and the signtool arguments it would use."""
    if require_trusted and self_signed:
        raise SigningPlanError(
            "Refusing a self-signed certificate when a trusted CA chain is required."
        )
    primary = (timestamp_url or DEFAULT_TIMESTAMP_URL).strip()
    fallback = (timestamp_fallback or FALLBACK_TIMESTAMP_URL).strip()
    timestamp_urls = [url for url in (primary, fallback) if url]
    # Preserve order while dropping a repeated URL.
    seen: set[str] = set()
    unique_urls: list[str] = []
    for url in timestamp_urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    azure = bool(azure_endpoint and azure_account and azure_profile)
    hsm = bool(csp_name and key_container)
    local_cert = bool(pfx_path or thumbprint or self_signed)
    if require_trusted and not (azure or hsm or pfx_path or thumbprint):
        raise SigningPlanError(
            "No trusted code-signing credential is configured. "
            "A PFX, a certificate thumbprint, an HSM container, or Azure Trusted Signing is required."
        )
    if not (azure or hsm or local_cert):
        return {
            "backend": "skip",
            "argv": [],
            "timestamp_urls": unique_urls,
            "reason": "no_signing_material",
        }

    target = str(file_path)
    if azure:
        if not dlib_path:
            raise SigningPlanError(
                "Azure Trusted Signing needs Azure.CodeSigning.Dlib.dll or the Invoke-TrustedSigning module."
            )
        argv = [
            "signtool",
            "sign",
            "/v",
            "/fd",
            "SHA256",
            "/tr",
            unique_urls[0],
            "/td",
            "SHA256",
            "/dlib",
            dlib_path,
            "/dmdf",
            "<metadata.json>",
            target,
        ]
        return {"backend": "azure_dlib", "argv": argv, "timestamp_urls": unique_urls, "reason": ""}

    if hsm:
        argv = ["signtool", "sign", "/fd", "SHA256", "/csp", csp_name, "/kc", key_container]
        if cert_file:
            argv.extend(["/f", cert_file])
        if unique_urls:
            argv.extend(["/tr", unique_urls[0], "/td", "SHA256"])
        argv.append(target)
        return {"backend": "hsm_csp", "argv": argv, "timestamp_urls": unique_urls, "reason": ""}

    return {
        "backend": "set_authenticode",
        "argv": [],
        "timestamp_urls": [] if self_signed else unique_urls[:1],
        "reason": "self_signed" if self_signed else "local_certificate",
    }
