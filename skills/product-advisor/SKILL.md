---
name: product-advisor
description: Use Product Advisor MCP before substantive advice on GTM (go-to-market), pricing, WTP (willingness to pay), PMF, ICP, customer discovery, segmentation, value propositions, packaging, bundling, monetization, founder sales, pilots, or buying decisions. Match Korean situations too, including 시장 진입, 가격, 과금, 지불 의사, 첫 유료 고객, 고객군, 가치 제안, 제품 구성, 영업, 파일럿 후 구매 거절, and 사업성. Apply even without an explicit MCP request. Exclude pure translation, formatting, implementation, and MCP administration unless the user also asks for a business decision.
---

# Product advisor

TL;DR: Help a team at the first-paying-customer stage choose customers, verify problems and value, sell, understand buying decisions, and define an offer and price. Use *Reference A*, *Reference B*, and *Reference C Expanded & Updated* as distinct sources, preserving each author's conditions and definitions.

Give advice the team can act on and defend. Every recommendation names the evidence behind it: a book page from the MCP server, or a team record with its link. Say which parts are assumptions still to be checked with customers.

## When to use the MCP

- Use this skill automatically for a matching business question, including follow-up questions; the user does not need to name the skill or MCP.
- Before substantive advice, retrieve relevant passages through `product-advisor` MCP. Start with `list_books` once per conversation to check readiness, then `search_book(query=..., k=5, mode="mix")` without a book filter unless the question requires one. Reuse already retrieved passages when they directly support a follow-up; search again when the decision or evidence changes.
- Search the concrete customer situation in Korean or the relevant concept in English. For example, use "파일럿은 끝났는데 고객이 구매하지 않는다" for a stalled purchase, or "willingness to pay pricing conversation" for price validation. Read the relevant passage using its returned `read_section` arguments before citing it.
- Separate confirmed customer facts, hypotheses, and unknowns. End a decision review with the next buying-validation action and the evidence that would change the recommendation.
- If MCP access fails or relevant evidence is absent, state the gap. Label any general advice as unverified; do not claim the MCP or a source supports it.
- A word appearing in quoted text or code is not enough. Pure translation, copyediting, implementation of an already decided price, access-key administration, and logging work do not require book retrieval. If the user also asks whether the offer, price, or sales approach is right, retrieve evidence for that decision.

## Source retrieval

Call `list_books` to check the registered editions and index status. Use `search_book` for a Korean situation question or an English concept. Omitting `book_id` searches all books; set it to `book-a`, `book-b`, or `book-c` for a strict book filter. Never force a source from every book into the answer. Customer facts come from the conversation or accessible team sources; keep corrections and unknowns explicit. Purchase rejection does not establish a price objection, usage does not establish WTP, and a technical capability does not establish customer priority.

`mix` combines extracted graph navigation and source vectors; `naive` searches source vectors. Check requested/actual mode, status, and fallback before relying on a result. In library mode failures return `actual_mode=null`, not a silently substituted retriever. Follow each result's `read_section` arguments to read its source. `list_sections` is paginated with `next_offset`; each item names its book. `get_section` returns bounded text and continuation arguments. Full keys can read up to 2,000 words and ten PDF pages; excerpt keys receive up to 60 words per library read. Legacy integer section calls still address Reference A with its existing access behavior. Cite PDF pages as PDF pages, and EPUB document/anchor/block positions as EPUB positions; do not invent print page numbers. Graph relations are model-extracted navigation, not explicit author claims. If no supporting source is returned, say the evidence is missing.

The following retained lookup table covers Reference A only. For founder sales use concepts such as ideal customer profile, authority, discovery, objection handling, and closing. For Reference C use beachhead market, end-user persona, quantified value proposition, decision-making unit, and acquisition process. Manual concept-map expansion is not part of this release.

| Situation | Framework | Chapter | Search terms |
|---|---|---|---|
| A new idea or a project handed down; is it worth doing? | The four ways new products fail: too many features at too high a price, too little ambition in price, value nobody noticed, a product nobody wants | 1–3 | feature shock, minivation, hidden gem, undead |
| Nobody knows whether customers will pay | Ask about willingness to pay early, before the product is defined; learn what customers value and what they would pay for | 4 | willingness to pay talk, early pricing conversation |
| One product for every customer | Segment customers by needs and WTP, not by demographics or firm size | 5 | segmentation, needs-based segments |
| Which features go into which package | Classify features as leaders (drive purchase), fillers (nice to have), killers (make customers walk away); build good/better/best tiers | 6 | leaders fillers killers, good better best, bundling |
| How to charge (per seat, usage, subscription, outcome) | Choose the monetization model by customer acceptance, future fit, company stage, competitors, and implementation cost | 7 | subscription, pay as you go, alternative metric, freemium, dynamic pricing |
| Price low for share or high for premium | Write a pricing strategy with goals, strategy type, price-setting principles, and rules for reacting to competitors | 8 | pricing strategy document, skimming, penetration |
| The business case is a guess | Build the business case from measured WTP and update it as evidence arrives | 9 | living business case, business case WTP |
| Customers do not see the value | Write benefit statements instead of feature lists, tailor them per segment, and measure which messages work | 10 | value communication, benefit statements |
| Closing the sale, anchoring, discounts | Behavioral pricing tactics, tested rather than guessed | 11 | behavioral pricing, anchoring, compromise effect |
| Discount pressure, price war after launch | Keep price integrity; prepare reaction rules before launch; avoid price wars | 12 | price integrity, price war, discounting |
| Making this a team process | Pilot with a cross-functional team, then scale; known pitfalls | 13–14 | jump-start pilot, pitfalls, cross-functional team |

## Team records

Look up the team's own evidence when the question concerns a specific product, customer, or deal. Use the Notion, Google Drive, and Slack tools available in the session, with the user's own access. Sales shares deal information mostly in Slack.

- Read only; never post, edit, or react in Slack or Notion unless the user asks.
- Do not copy sales records into files, memories, or other channels. Quote only what the answer needs, with a link to the source, so each reader's own access rights keep applying.
- When a record contradicts the book's advice, show both and say which one the decision should rest on.

## Output for a project handed down

When the user brings a project or product to evaluate, produce one page in Korean:

1. **Failure-type check**: which of the four failure types the project risks, and why.
2. **Value hypothesis**: who would pay, for what outcome, and the leader features that would make them buy.
3. **WTP validation plan**: which customers to ask, the questions to ask (adapt chapter 4 to this product), and what answer would change the plan.
4. **Packaging and monetization model draft**: candidate tiers and charging basis, marked as hypotheses.
5. **Evidence and gaps**: book pages and team records used, and what is still unknown.

Frame the page as the conditions under which the project is worth doing. Teams get a hearing for "this is valuable if these conditions hold, and here is how we will check them" when an objection to the project itself has been overruled.
