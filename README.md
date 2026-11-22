# product-advisor

TL;DR: Product Advisor searches three source books for customer selection, value validation, founder sales, purchase decisions, packaging, and pricing. A versioned LightRAG library serves source evidence through the existing authenticated MCP endpoint. Search defaults to all books and `mix`; `naive` remains available for comparison.

The repository and Docker image contain no book files. Private PDFs, EPUB, source catalogs, indices, keys, and caches stay in the mounted `data/` directory. See [three-book operation and verification](docs/three-book-library.md).

## What it provides

| Part | What it does |
|---|---|
| Skill `product-advisor` | Loads on pricing, WTP, PMF, GTM, packaging, and sales topics in Korean or English; maps situations to frameworks; points Claude to the team's records in Notion, Google Drive, and Slack; and defines a one-page evaluation for a project handed down without a business case |
| MCP server `product-advisor` | Lists, searches, and reads the reference material in Korean or English; each result names its page and section |
| Keys | One key per person with an expiry and a scope: `full` for whole pages and sections, `excerpt` for short passages and summaries |

The server keeps no team records or query history. Search requests go to OpenRouter for embedding and reranking but are never indexed. The three-book library returns book-specific source chunks and locations without generating an answer. Model-extracted graph links support navigation; they are not author quotations or verified customer facts. Manual concept maps, company ingestion, customer memory, and CRM are outside this release. `retrieve_decision_context` remains callable for compatibility and returns source evidence with no manual framework cards in library mode.

## Quick start

For the three-book library, prepare and build an isolated release, seal it, run the frozen evaluations, and stage it using `scripts/deploy-library.py`. Activate it on the existing host and accept only after real MCP calls pass. [The library guide](docs/three-book-library.md) includes the commands and rollback procedure. The single-PDF command below remains the legacy installation path; do not use it to update an active three-book release.

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
