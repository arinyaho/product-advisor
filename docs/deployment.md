# Deployment

The MCP server runs in one Docker container on a host the team can reach, a Synology NAS in this setup. `scripts/nas.sh` deploys and operates it from any machine that can ssh into that host and has a clone of this repository; the only other inputs are the book's PDF and an OpenRouter key. Users install only the Claude Code plugin and point it at the server with their own key.

## Prerequisites

| Where | Requirement |
|---|---|
| Deploying machine | A clone of this repository, `bash`, `ssh`, `tar`, and the book's PDF |
| Host (Synology) | DSM 7.2 or later with Container Manager (it provides `docker compose`), SSH enabled in Control Panel → Terminal & SNMP, and an administrator account that can run `sudo docker` |
| OpenRouter | A key made for this server only, with a credit limit and a daily reset. The server's use is a few cents for the first build and well under a cent a day after that |
| Each user | Claude Code and `uv` |

The OpenRouter account's data policy decides which models the key may call. The server needs `qwen/qwen3-embedding-4b` and `anthropic/claude-haiku-5.5`; both pass a policy that admits only providers that do not retain data.

## Deploy

```sh
OPENROUTER_API_KEY=<key for this server> scripts/nas.sh admin@nas.local deploy path/to/book.pdf
```

The script:

1. Copies `mcp/`, `docker-compose.yml`, and `.env.example` to `NAS_DIR` (default `/volume1/docker/product-advisor`) and the PDF to `data/book.pdf`.
2. Writes `.env` with the OpenRouter key on the first deploy only (mode 0600). Later deploys leave it alone, so the key does not need to be set again.
3. Writes `deploy.env` with the owner of `data/` as the container's user, so the container can write keys and caches there, and the port.
4. Builds and starts the container with `sudo docker compose`. ssh allocates a terminal for the sudo password prompt.
5. Waits until the logs show `section summaries ready`, about three minutes on the first start and a few seconds when the caches already match. It stops early with the last log lines if the container exits or restarts.
6. Issues a `full` key named `owner`, valid for a year, if no key exists yet, and prints it once.

Run the same command to update the server after pulling new code. The PDF is copied again, and the caches are rebuilt only if the text, chunking, prompt, or models changed.

| Variable | Default | Meaning |
|---|---|---|
| `NAS_DIR` | `/volume1/docker/product-advisor` | Project directory on the host |
| `DOCKER` | `sudo /usr/local/bin/docker` (`docker` for `local`) | Docker command on the host. DSM keeps `docker` outside the PATH of ssh commands and of `sudo` |
| `PORT` | `8765` | Host port, bound to 127.0.0.1 |
| `OPENROUTER_API_KEY` | none | Required on the first deploy only |

Use `local` as the host to deploy on the machine running the script, for example on the NAS itself.

## Reverse proxy

The container listens on `127.0.0.1:PORT` only. Publish it through DSM's reverse proxy, which terminates HTTPS:

1. Control Panel → Login Portal → Advanced → Reverse Proxy → Create.
2. Source: protocol HTTPS, the hostname users will reach (for example `advisor.example.com`), port 443.
3. Destination: protocol HTTP, hostname `localhost`, port `8765`.
4. Control Panel → Security → Certificate: assign a certificate to that hostname.

The MCP endpoint is then `https://advisor.example.com/mcp`. Keep the port closed on the router for anything but 443 to the NAS.

## Keys

The README's [Keys](../README.md#keys) section covers issuing, renewing, revoking, and listing keys.

## Install for a user

The README's [Set up a client](../README.md#set-up-a-client) section covers Claude Code and Codex.

The skill reads team records through each user's own Notion, Google Drive, and Slack access; nothing about them is configured on the server.

## Operate

| Command | Use |
|---|---|
| `scripts/nas.sh HOST logs` | Last 50 log lines: build progress, OpenRouter errors, requests |
| `scripts/nas.sh HOST status` | Container state |
| `scripts/nas.sh HOST down` | Stop and remove the container; `data/` and `.env` stay |

| Symptom | Cause and fix |
|---|---|
| Users get 401 | The key is wrong, expired, or revoked: `list-keys`, then `renew-key` or `issue-key` |
| Search results say `"semantic": false` | The index is still building or OpenRouter is failing; `logs` shows `embedding index not built yet` with the error. Check the OpenRouter key's credit limit and data policy |
| `get_section` with an excerpt key says summaries are being prepared | The summaries build is running or failing; `logs` shows `section summaries not built yet` with the error |
| Deploy stops with "the container stopped or is restarting" | The printed log lines show the exception; a wrong PDF path inside the container or an unwritable `data/` are the usual causes |
| `sudo: a terminal is required` | Use an ssh destination that can allocate a terminal, or set `DOCKER=docker` for an account in the docker group |

## Rotate the OpenRouter key

Create a new key in OpenRouter, replace the value in `NAS_DIR/.env` on the host, run `scripts/nas.sh HOST deploy BOOK.pdf` to restart, and delete the old key in OpenRouter.
