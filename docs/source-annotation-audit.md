# Pilot-question source annotation audit

TL;DR: The frozen v1 all-pilot-ko expected-source list omits directly relevant passages. Its 14/15 failure remains recorded. The user authorized the source-reviewed correction on 2026-11-23. The five passages below are added only in eval/three_book_cases_v2.json; the frozen v1 fixture and its failures remain unchanged. Neither fixture constitutes held-out quality evidence.

- Question: 파일럿은 끝났고 사용자는 좋아하지만 고객이 구매하지 않는다. 가격을 내리기 전에 지불 의사, 실제 구매자, 예산과 구매 장애를 무엇으로 확인해야 하나?
- Frozen fixture SHA-256: `2ee407af9803cd52948dacaa0cfff806bd985add81e5136ea8d41226187001dd`. Git HEAD and the working fixture are byte-identical.
- The original expected list contains 35 chunk IDs. The issue is source-label coverage, not a claim that every retrieved passage is relevant.
- Keep the original v1 question, labels, results, k=5, score floor 0.15, and 4,000-token limit unchanged. The correction is recorded in the separate v2 annotation version. Rerun both modes and actual MCP acceptance with an explicit fixture argument; do not reuse v1 passing receipts.

| Reviewed additional source | Book / location | Direct support |
|---|---|---|
| `src-cbbdad9d16fdfc2a4d49d83e` | book-a / PDF 66 | Direct customer WTP research: overall price range, feature WTP, and five types of research questions. |
| `src-e5422b82e8066ebee7dc67a5` | book-a / PDF 61 | Concrete acceptable/expensive/prohibitively-expensive price questions, purchase questions, and follow-up why. |
| `src-bb147357f161ca364defb8f7` | book-a / PDF 64 | Validate customer pain and value, then ask acceptable price; distinguish value validation from a price guess. |
| `src-32aa956671106b45b231b880` | book-b / PDF 237 | Qualify whether the organization buys tools and knows a purchasing process; distinguish no-budget objections from disqualification. |
| `src-60b5110ea889b84511c82383` | book-c / OPS/c16.xhtml#c16-li-0006; block 15 | Check DMU purchasing-authority limits and the purchase process to understand budget-related price friction. |

- The deployed b7c candidate was rejected after live acceptance failed; rollback was confirmed through the original MCP tools and legacy section 27.
- Qwen query-only profiles reuse the same source catalog, graph and embeddings with zero new paid index calls. The original v1 exact-ID check still fails for the pilot question, despite the relevant passages above.
- Comma-list reranking was tested as an experiment; it did not fix the frozen ID check and is not evidence of improved retrieval quality. Do not mark that experiment successful.
- The v1 fixture SHA-256 remains unchanged. V2 adds exactly these five IDs to all-pilot-ko; all other labels, questions and negative cases are identical. The deployment gate binds the evaluation and live acceptance to the same fixture hash.
- V2 fixture SHA-256: `f484d8e31fe3152518ab4a6f79ef1c862a9f5d44d2edc392fdd48fcae5a6c4f7`.
- This is a post-hoc correction of incomplete source annotations, not evidence of a retrieval improvement or general GraphRAG superiority.
