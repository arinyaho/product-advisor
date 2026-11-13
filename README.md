# product-advisor

A Claude Code plugin that advises a product team whenever a conversation turns to pricing, willingness to pay, product-market fit, go-to-market, packaging, or sales. A skill maps the situation to established frameworks and tells Claude when to look things up; an MCP server on one Docker host searches and reads the reference material for anyone holding a key.

The repository holds no reference text. The PDFs, the key store, and the caches live only in the server's `data/` directory.

## What it provides

| Part | What it does |
|---|---|
| Skill `product-advisor` | Loads on pricing, WTP, PMF, GTM, packaging, and sales topics in Korean or English; maps situations to frameworks; points Claude to the team's records in Notion, Google Drive, and Slack; and defines a one-page evaluation for a project handed down without a business case |
| MCP server `product-advisor` | Lists, searches, and reads the reference material in Korean or English; each result names its page and section |
| Keys | One key per person with an expiry and a scope: `full` for whole pages and sections, `excerpt` for short passages and summaries |

The server keeps no team data and no queries; customer, deal, and hypothesis records stay in the team's own systems.

## Quick start

Deploy from any machine that can ssh into a Docker host, with the reference PDF and an OpenRouter key made for this server:

```sh
OPENROUTER_API_KEY=<key> scripts/deploy.sh user@host deploy path/to/reference.pdf
```

The script copies the server and the PDF, builds and starts the container, waits until the server is ready (about three minutes the first time), and prints a `full` key for you. Publish `127.0.0.1:8765` on the host through a reverse proxy that terminates HTTPS.

[docs/deployment.md](docs/deployment.md) covers prerequisites, the reverse proxy, and troubleshooting.

## Keys

Each person gets their own key. Run these from a clone of this repository on a machine that can ssh into the host.

```sh
scripts/deploy.sh user@host issue-key alice --days 30                # excerpt scope, the default
scripts/deploy.sh user@host issue-key bob --days 365 --scope full    # whole pages and sections
scripts/deploy.sh user@host renew-key alice --days 30
scripts/deploy.sh user@host revoke-key alice
scripts/deploy.sh user@host list-keys
```

`issue-key` prints the key once; the server stores only its hash. Issuing a key for an existing name replaces that person's key. Send the key to the person over a private channel, such as a direct message, never in a shared channel or a repository. Changes apply on the next request, with no restart.

## Set up a client

Each user needs read access to this repository, the server's URL (`https://<host>/mcp`), and their own key.

### Claude Code

1. Add the URL and the key to the `env` block of `~/.claude/settings.json`:

   ```json
   {
     "env": {
       "PRODUCT_ADVISOR_MCP_URL": "https://<host>/mcp",
       "PRODUCT_ADVISOR_MCP_KEY": "<your key>"
     }
   }
   ```

2. Install the plugin, which brings the skill and the MCP server:

   ```sh
   claude plugin marketplace add arinyaho/product-advisor
   claude plugin install product-advisor@product-advisor
   ```

3. Check that `claude mcp list` shows `plugin:product-advisor:product-advisor` as connected.

### Codex

1. Export the key in the shell Codex starts from, for example in `~/.zshrc`:

   ```sh
   export PRODUCT_ADVISOR_MCP_KEY=<your key>
   ```

2. Register the MCP server. Codex reads the key from that variable on each start:

   ```sh
   codex mcp add product-advisor --url https://<host>/mcp --bearer-token-env-var PRODUCT_ADVISOR_MCP_KEY
   ```

3. Install the skill by linking it from a clone of this repository. Run `git pull` in the clone to update the skill:

   ```sh
   git clone https://github.com/arinyaho/product-advisor.git ~/product-advisor
   ln -s ~/product-advisor/skills/product-advisor ~/.codex/skills/product-advisor
   ```

4. Check that `codex mcp list` shows `product-advisor` as enabled.

## Documentation

| Document | Content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, indexing and search data flow, tools, access control and threats, failure handling, caches, design decisions |
| [docs/deployment.md](docs/deployment.md) | Deploying to a Docker host, reverse proxy, keys, operations |
| [docs/experiments.md](docs/experiments.md) | The experiments behind the design and the evaluation methods |
| [eval/README.md](eval/README.md) | Running the evaluations and comparing a change |

## Test

```sh
cd mcp
BOOK_PDF=/path/to/reference.pdf OPENROUTER_API_KEY=<key> \
  uv run --python 3.13 --with-requirements requirements.txt --with httpx python test_server.py
```

The tests run against a real PDF and OpenRouter. Set `DATA_DIR` to a directory that already holds the caches to skip the three-minute build.

## License

[MIT](LICENSE)
