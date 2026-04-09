# Future TODOs

## Reverse port forwarding (SSH `-R`)

Forward direction (`-L`) is shipped: `localhost:LOCAL → sandbox:REMOTE`. The
inverse — `sandbox:REMOTE → localhost:LOCAL`, so processes inside the sandbox
can dial services running on the host — is not yet supported.

**Use cases:**
- Sandbox connects to a local Postgres / Redis instead of running one in Modal.
- OAuth/webhook callbacks where the redirect URL must point at `localhost`.
- LLM agent in the sandbox calling a tool server you're iterating on locally.
- Routing sandbox traffic through a local proxy (e.g. mitmproxy) for inspection.

**Sketch:** mirror of the existing `Option B` transport. Run a TCP listener
*inside* the sandbox via `socat TCP-LISTEN:PORT,fork EXEC:...` (or a small
Python relay daemon), then for each accepted connection on that listener,
pipe bytes back through a `sandbox.exec` stdin/stdout pair to a local TCP
dial on the host. Slightly fiddlier than `-L` because the listener lives on
the remote side, so the relay topology is flipped.

**CLI shape (proposed):**
```
modal-sprite attach my-box -R 5432            # sandbox:5432 -> localhost:5432
modal-sprite attach my-box -R 5432:5433       # sandbox:5432 -> localhost:5433
```

**Why deferred:** `-L` covers ~90% of dev workflows, and the reverse direction
has a UX gotcha — processes inside the sandbox have to know to dial a magic
host/port that the relay is listening on, which is less transparent than `-L`.
Worth doing once someone actually misses it.
