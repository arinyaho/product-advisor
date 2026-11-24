# Product Advisor

TL;DR: Product Advisor helps teams working toward their first paying customer and a repeatable product business examine customer selection, value, founder sales, buying decisions, packaging, and pricing. A skill combines the user's context with source evidence retrieved from three books through an authenticated MCP server.

## Private reference library

The private library covers willingness to pay, packaging, pricing, founder sales, customer selection, value propositions and buying decisions. Titles, authors, editions, filenames and source-specific identifiers are configured outside Git and returned only through the authenticated MCP service.

Book files and registration metadata are private runtime inputs, not part of this repository or its license. The repository and Docker image contain no books, credentials, or indices. A server operator must supply authorized sources; cloning this repository alone does not provide book access.

## Use it

1. Obtain the HTTPS MCP URL and your personal key from the server operator.
2. [Connect a client](#connect-a-client) and install the skill.
3. Ask the assistant to call `list_books()` and verify the three book IDs and `index_status=ready`.
4. Ask a concrete situation question, then read the supporting passage before treating a recommendation as established.

Example questions:

- "파일럿은 끝났는데 고객이 구매하지 않는다. 다음 미팅에서 무엇을 확인해야 할까?"
- "누구를 첫 고객군으로 좁혀야 할지 모르겠다."
- "사용자는 좋아하지만 예산 결정권자를 모른다."
- "첫 고객에게 어떤 범위와 가격으로 제안해야 할까?"
- "Which features are leaders, fillers, or killers for this customer segment?"

The skill helps interpret evidence; the MCP server returns source passages rather than generating an answer. Customer facts come from the conversation or team records the assistant can access separately. Purchase rejection does not establish a price objection, and usage does not establish willingness to pay. A result from every book is not required.

### Automatic skill selection

The skill description matches GTM, pricing, WTP, PMF, ICP, customer discovery, packaging, founder sales, and buying decisions, including Korean situation questions. It instructs the assistant to retrieve MCP evidence before substantive business advice, even when the user does not mention the MCP. Directly relevant evidence can be reused for follow-ups. Pure translation, formatting, implementation of an already decided price, and MCP administration are outside this routing scope.

This is implicit skill invocation, not a hook. The client chooses the skill from its description; `agents/openai.yaml` explicitly permits implicit invocation and declares the existing `product-advisor` MCP dependency. Selection is model-driven, not a guaranteed keyword rule. Use `$product-advisor` in Codex when explicit invocation is needed. Codex detects skill edits automatically; restart Codex if an edit does not appear. Other clients must update their installed plugin. See the [official skill documentation](https://learn.chatgpt.com/docs/build-skills).

## MCP tools

| Tool | Behavior |
|---|---|
| `list_books()` | Titles, authors, verified/unknown editions, source hashes, index version and readiness. |
| `search_book(query, k=5, book_id=None, mode="mix")` | Searches all three books by default. A book filter selects its own graph and vector stores before retrieval. Korean situations and English concepts are supported; at most 20 requested chunks and 4,000 tokens including metadata. |
| `list_sections(book_id=None, offset=0, limit=100)` | Book-identified contents with `total` and `next_offset`; all books when the filter is omitted. |
| `get_section(id, start_page=None, book_id=None)` | Bounded source text and continuation arguments. Existing numeric IDs still identify the same Reference A sections. |

Use `book_id="book-a"`, `"book-b"`, or `"book-c"` to constrain retrieval. Follow a hit's `read_section` arguments, then the returned continuation to read more. PDF locations are PDF pages; EPUB locations identify document, anchor, block and word range. Unverified printed pages remain null.

`mix` uses LightRAG graph navigation and source vectors; `naive` uses vectors for comparison. Check `requested_mode`, `actual_mode`, `status`, and `fallback` before relying on results. Failure returns `actual_mode=null`, `status=unavailable`, and `fallback=false`. Graph links are model-extracted or derived navigation, not author quotations or verified customer facts. Search excludes index/contents and heading-only chunks before reranking; their text and contents remain readable.

## Connect a client

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

1. Supply the key through your secret manager or a hidden terminal prompt in the shell that launches Codex. For example, in Bash:

   ```bash
   read -r -s -p 'Product Advisor key: ' PRODUCT_ADVISOR_MCP_KEY
   export PRODUCT_ADVISOR_MCP_KEY
   printf '\n'
   ```

2. Register the MCP server. Codex reads the key from that variable on each start:

   ```sh
   codex mcp add product-advisor --url https://<host>/mcp --bearer-token-env-var PRODUCT_ADVISOR_MCP_KEY
   ```

3. Install the skill by linking it from a clone of this repository. Run `git pull` in the clone to update the skill:

   ```sh
   git clone https://github.com/arinyaho/product-advisor.git ~/product-advisor
   mkdir -p ~/.codex/skills
   ln -s ~/product-advisor/skills/product-advisor ~/.codex/skills/product-advisor
   ```

4. Check that `codex mcp list` shows `product-advisor` as enabled.

Codex must inherit that environment variable when it starts. See the [official OpenAI MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Never commit a key or put its value in a command argument. Keep Claude's private settings file outside Git. After either client is connected, verify actual `list_books`, search and source-reading calls; a configured server alone does not prove it is usable.

## Access and data flow

- Each person has a separate expiring key. Library `full` reads return up to 2,000 words and ten PDF pages; `excerpt` reads return up to 60 words. Legacy numeric Reference A calls retain their existing full-text/summary behavior.
- The server does not index team records or persist query history. OpenRouter receives queries for multilingual embeddings and candidate passages for reranking. Offline graph extraction also uses OpenRouter. Original Korean and English queries are not rewritten or translated.
- Team records are read through the assistant's separately authorized connectors. They are not part of the book index.
- Raw books, catalogs, indices, keys, extraction caches and private evaluation receipts stay in mounted `data/` storage. Keys are stored as hashes; replacing a key for an existing name invalidates that person's previous key.
- Manual concept maps, company ingestion, customer memory and CRM are outside the three-book release. `retrieve_decision_context` remains callable for compatibility and returns no manual framework cards in library mode.

## Operate the server

Use the [three-book library guide](docs/three-book-library.md) to prepare an isolated release, build or reuse an index, verify source locations, seal it, evaluate it, and stage it. Activation preserves the old image and data and requires acceptance through real MCP calls; failure or timeout restores the previous service. A health check alone is not deployment acceptance.

The single-PDF `scripts/deploy.sh ... deploy ...` command is the legacy installation path. Do not use it to update an active three-book library. Existing key-management commands remain available to operators:

```sh
scripts/deploy.sh user@host issue-key alice --days 365 --scope full
scripts/deploy.sh user@host renew-key alice --days 365
scripts/deploy.sh user@host revoke-key alice
scripts/deploy.sh user@host list-keys
```

`issue-key` prints the new key once. Deliver it through a private channel. Key changes apply to the next request without a service restart.

## Validation and limits

The deployed release passed 15 development questions in each mode and 12 actual MCP acceptance checks. The v2 annotations explicitly add five source-reviewed passages omitted from v1; the original 14/15 failures remain recorded. Passing means at least one expected source was returned, not that every result is relevant or that GraphRAG is better than vector retrieval. See the [validation record](docs/three-book-validation.md) for the exact deployed profile, model versions, costs, latency and rollback evidence.

The follow-up body-evidence filter has its own [regression cases](eval/three_book_evidence_cases.json) and private evaluation receipts; it is separate from the recorded production deployment until explicitly released. See [evaluation instructions](eval/README.md) for provider-backed comparisons and local tests. Image-only EPUB text is not OCRed, and unverified edition/year fields remain explicitly marked.

## Documentation

| Document | Content |
|---|---|
| [Three-book library](docs/three-book-library.md) | Sources, tools, models, build, cache reuse, deployment and rollback |
| [Validation and deployment](docs/three-book-validation.md) | Verified editions/scope, actual MCP evidence, costs, failures and limitations |
| [Evaluation](eval/README.md) | Fixed questions, body-evidence checks and comparison commands |
| [Source annotation audit](docs/source-annotation-audit.md) | Explicit v1-to-v2 correction and retained failures |
| [Architecture](docs/architecture.md) | Component responsibilities and data flow |
| [Host operations](docs/deployment.md) | Docker host prerequisites, reverse proxy and key management |
| [Earlier experiments](docs/experiments.md) | Historical single-book experiments |

## License

[MIT](LICENSE) covers this repository's code. The reference books have their own rights and are not distributed here.
