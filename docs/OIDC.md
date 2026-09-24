# OIDC integration seam

Memoratum keeps token verification outside the core server. An embedding
application supplies an `IdentityProvider` to `create_app`; the provider must
validate the token signature, issuer, audience, expiry, and authorized claims
before returning an `Identity`:

```python
from memoratum.app import create_app
from memoratum.auth import Identity


class MyProvider:
    def verify(self, token: str) -> Identity | None:
        # Validate with the application's OIDC/JWT library and cache keys.
        claims = validate_with_your_identity_provider(token)
        if claims is None:
            return None
        return Identity(
            subject=claims.subject,
            container_tag=claims.container_tag,
            org_id=claims.organization_id,
        )


app = create_app(identity_provider=MyProvider())
```

The server maps a verified subject to a local key fingerprint, enforces the
returned container/organization scope, and records `actorKind: "oidc"` in the
local audit trail. The raw token is never stored or returned. A verifier
exception or invalid token is treated as unauthorized.

`MEMORATUM_OIDC_ISSUER`, `MEMORATUM_OIDC_AUDIENCE`, and
`MEMORATUM_OIDC_JWKS_URL` are configuration seams for the embedding provider;
the core process does not fetch JWKS or call an identity endpoint on its own.
This keeps the no-telemetry boundary explicit and lets deployments choose their
own signing, caching, revocation, and rotation policy.

`StaticIdentityProvider` exists only for tests and local development. Do not
use it as a production verifier.
