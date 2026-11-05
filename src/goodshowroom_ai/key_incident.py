"""Trusted operator response. Deliberately has no HTTP or signed-token entry."""

from uuid import UUID

from cryptography.hazmat.primitives.serialization import load_pem_public_key

from goodshowroom_ai.identity_security import public_key_fingerprint
from goodshowroom_ai.storage.models import (
    AppAuditEvent,
    InternalAuthorizationContext,
    StorageCapability,
    utc_datetime,
)


async def contain_signing_key_compromise(
    db, verifier, memory, *, kid, actor_id, request_id, expected_policy_version, now
):
    """Stop work first. Commit the tombstones, global cutoff and audit together.

    The caller must be an owner-authorized trusted operator. A request assertion
    cannot authorize incident recovery. Keep services stopped on any failure.
    Resume with a new verifier/store only after a confirmed durable commit.
    """
    verifier.lock()
    await memory.close()
    if not isinstance(actor_id, UUID) or not isinstance(request_id, UUID):
        raise ValueError("Incident operation is unavailable.")
    metadata = verifier.keys.get(kid)
    if metadata is None:
        raise ValueError("Incident operation is unavailable.")
    fingerprint = public_key_fingerprint(load_pem_public_key(metadata.public_pem))
    now = utc_datetime(now)
    context = InternalAuthorizationContext(
        actor_id=actor_id,
        capabilities=frozenset(
            {
                StorageCapability.READ_POLICY,
                StorageCapability.REVOKE_IDENTITY_KEYS,
                StorageCapability.ADVANCE_ACCESS_CONTROL,
                StorageCapability.WRITE_REQUIRED_AUDIT,
            }
        ),
    )
    async with db.identity_incident_transaction(context) as tx:
        await tx.advance_access_control_fence(expected_policy_version)
        state = await tx.revoke_identity_key(kid=kid, fingerprint=fingerprint, now=now)
        await tx.append_required_audit(
            AppAuditEvent(
                actor_id=actor_id,
                target_id=None,
                action="security_outcome",
                outcome="success",
                request_id=request_id,
                created_at=now,
            )
        )
    # An unknown commit or readback failure is not success. Retry while stopped.
    if await db.load_identity_security() != state:
        raise RuntimeError("Incident confirmation is unavailable.")
    return state
