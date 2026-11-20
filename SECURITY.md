# Security

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it privately through GitHub's
private vulnerability reporting: the repository's **Security** tab, then **Report a vulnerability**.

Say which version you ran (`waypointd --version`), what you did, what happened, and what an attacker
could gain. Leave out tokens, addresses and other details of your own deployment unless they are
needed to understand the problem.

We aim to acknowledge a report within a week. Fixes are made in the development repository and
published here with the next release; the report is credited in the release notes if you wish.

## Supported versions

Only the latest release gets fixes. Upgrading is described in [docs/INSTALL.md](docs/INSTALL.md).

## What Waypoint protects, and what it leaves to you

- Every request but `GET /v1/health` needs a client token. Tokens are random, stored only as hashes,
  rotatable and revocable; roles limit what each client may do.
- The daemon listens on `127.0.0.1` by default and refuses `0.0.0.0` and `::`. It speaks **plain
  HTTP**: reaching it from other machines safely is up to the deployment (an encrypted network, an
  SSH tunnel or a TLS proxy on the same machine; see "Network exposure" in docs/INSTALL.md).
- The daemon never runs a command; the deliverer only calls `tmux` with a fixed argument list. With
  `message = "ref"`, no text written by another client is typed into an agent's session.
- The content check refuses text that looks like secrets, addresses or device ids. It is a net for
  mistakes, not a guarantee: a shape it misses is welcome as an ordinary issue.
- `GET /v1/health` tells anyone who can reach it the version, by design.

Out of scope: problems that need what the operator already has (the database file, the
configuration, a coordinator's or human's token), and deployments that send Waypoint's plain HTTP
across networks they don't trust.
