# Security Policy

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/rockyevans-eng/mshroom/security/advisories/new)
rather than opening a public issue.

## What not to include

Never include real patient data (PHI) of any kind in a security report,
reproduction case, or attached file -- use synthetic data only, following
the same rule that applies to the rest of this project (see
`CONTRIBUTING.md`).

## Scope notes

MSHroom's Listener accepts arbitrary TCP connections and classifies
non-HL7 traffic (port scans, HTTP/TLS probes, junk) without parsing it as
a message, so it can safely sit on a port that gets scanned. Both the web
UI (`uvicorn ... --host`) and the Listener's bind address are
configurable; if you run MSHroom anywhere reachable beyond your own
machine, treat the bind host/port and the network you expose them to as
your own responsibility.
