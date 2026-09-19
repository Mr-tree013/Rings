# ADR-0004 — The Personal Vault Lives Outside the Repository

## Title

Keep the personal Vault physically separate from the code repository; treat removable
storage as archive, not runtime.

## Status

Accepted

## Context

The Vault holds personal material (facts, attachments, archived mail and documents).
The repository holds code, rules, playbooks and evaluations, and is expected to be
versioned in Git. Mixing them risks committing personal data, makes the repository grow
without bound, and couples document history to code history. Some material will live on
removable or mounted storage that can disappear between runs, so paths are not stable
identities.

## Decision

- The Vault lives outside the repository, e.g. `~/personal-vault/`, containing
  `facts/`, `inbox/`, `attachments/`, `archive/`.
- The repository never contains a real `vault/`, `secrets/` or `state/` directory.
- Cold archive storage (including USB drives) is referenced only through stable logical
  URIs such as `vault://archive-main/...`. Mount paths and Windows drive letters are
  never used as persistent identifiers.
- Removable storage is archive storage, never the Agent runtime.

## Alternatives Considered

- **Vault inside the repository**: simplest relative paths, but risks leaking personal
  data into Git and bloats the repository. Rejected.
- **Vault on a mounted USB drive as the working location**: tempting for capacity, but
  the drive is not always attached and mount paths change. Rejected as runtime; accepted
  as archive behind logical URIs.

## Consequences

- The Vault path is configuration, resolved at runtime from
  `~/.config/growing-assistant/`, never hard-coded.
- Backup, permissions and encryption of the Vault are the owner's responsibility and are
  documented separately from repository practice.
- Archive references can be resolved to different physical media without changing any
  stored identifier.

