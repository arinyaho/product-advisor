"""Hand-labeled question sets. Each question has one expected answer, so a better answer elsewhere counts as a miss;
see docs/experiments.md for why the pooled evaluation replaced these as the deciding measure."""

# Situations with their Korean and English phrasing and the PDF page range of the expected section.
SITUATIONS = [
    ("고객과 가격 얘기는 개발 초기에 언제 꺼내야 하나", "When should we start talking about price with customers during development?", 55, 68),
    ("기능을 너무 많이 넣었더니 비싸고 복잡해서 안 팔린다", "We packed in so many features that the product is expensive and complex, and it doesn't sell", 31, 36),
    ("경쟁사가 가격을 내리면 어떻게 대응해야 하나", "How should we respond when a competitor cuts its price?", 165, 176),
    ("우리 제품이 너무 싸게 팔려서 받을 수 있는 돈을 못 받고 있다", "We sell our product too cheaply and leave money on the table", 36, 39),
    ("고객 유형마다 원하는 게 달라서 하나의 제품으로는 안 맞는다", "Different customer types want different things, so one product doesn't fit all", 69, 78),
    ("어떤 기능을 기본 요금제에 넣고 어떤 기능을 상위 요금제에 넣을까", "Which features go in the basic plan and which in the premium plan?", 79, 94),
    ("구독으로 팔지 사용량 기반으로 과금할지 고민이다", "Should we sell as a subscription or charge by usage?", 95, 112),
    ("고객이 우리 제품의 가치를 모르고 기능 목록만 보고 판단한다", "Customers don't see our product's value and judge it only by the feature list", 137, 150),
    ("영업에서 할인 요청이 너무 많이 들어온다", "Sales keeps getting too many discount requests", 165, 176),
    ("아무도 원하지 않는 제품을 만들고 있는 것 같다", "I think we are building a product nobody wants", 43, 47),
    ("사업계획서 매출 전망이 근거 없는 추정이다", "The revenue forecast in our business plan is a guess with no evidence", 127, 136),
]

# Questions aimed at short sections and sections that start mid-page, keyed by the expected section's title.
# They were written from the section titles, which favors any method that embeds titles.
BOUNDARY = [
    ("The Good News: Monetizing Innovation Failures Come in Only Four Varieties", "신제품 수익화 실패는 몇 가지 유형으로 나뉘나"),
    ("These Four Monetizing Innovation Failures Can Be Avoided", "네 가지 수익화 실패는 피할 수 있나"),
    ("The Information You Need from Those Early Pricing Talks", "초기 가격 대화에서 고객에게서 어떤 정보를 얻어야 하나"),
    ("Typical Pitfalls of Segmentation", "고객 세분화에서 흔히 저지르는 실수는 무엇인가"),
    ("Microsoft Office: A Bundling Blockbuster", "Microsoft Office가 묶음 판매로 성공한 이유"),
    ("Principle #1: Leaders, Fillers, and Killers", "기능을 leader, filler, killer로 나누는 원칙"),
    ("Principle #2: Creating Good, Better, and Best Options", "good, better, best 세 가지 옵션을 만드는 방법"),
    ("How You Charge Trumps What You Charge", "얼마를 받느냐보다 어떻게 받느냐가 중요한 이유"),
    ("3. Market-Based Pricing: Auctions", "경매 방식 가격은 언제 맞나"),
    ("1. How Likely Are Your Customers to Accept the Model?", "고객이 새 과금 모델을 받아들일지 어떻게 판단하나"),
    ("3. What Stage Is Your Company In and Does Your Model Choice Fit That?", "회사의 성장 단계에 따라 과금 모델 선택이 달라지나"),
    ("4. What Are Your Competitors Doing?", "과금 모델을 정할 때 경쟁사를 어떻게 봐야 하나"),
    ("Building Block #3: Develop Price-Setting Principles", "가격 설정 원칙으로 무엇을 정해 두어야 하나"),
    ("Step 2: Make Your Benefit Statements Segment-Specific", "효익 메시지를 세그먼트별로 다르게 만들어야 하는 이유"),
    ("Appealing to the Early Adopters: Porsche’s Skimming Strategy", "Porsche는 얼리어답터에게 어떤 가격 전략을 썼나"),
    ("Creating Winning Sales and Marketing Messages", "Dräger는 판매와 마케팅 메시지를 어떻게 만들었나"),
    ("Expanding the Product Portfolio", "Optimizely는 제품군을 어떻게 넓혔나"),
    ("To Bundle or Not to Bundle", "Optimizely는 묶음 판매를 할지 말지 어떻게 정했나"),
    ("The Next Steps in Swarovski’s Monetization Journey", "Swarovski의 수익화는 다음에 어디로 가나"),
]

# Questions that need several sections, with the section titles a complete answer covers.
SYNTHESIS = [
    ("이 책이 말하는 신제품 수익화 실패 유형을 모두 정리해 줘", ["Flavor "]),
    ("가격 전략 문서에 들어가야 할 요소를 전부 알려줘", ["Building Block #"]),
    ("과금 모델에는 어떤 종류가 있고 무엇을 기준으로 골라야 하나",
     ["1. The Subscription", "2. Dynamic", "3. Market-Based", "4. Alternative", "5. Freemium", "Five Questions"]),
    ("새 수익화 프로세스를 회사에 도입할 때 흔히 빠지는 함정을 모두 알려줘",
     ["1. Putting", "2. Not Forming", "3. Banking", "4. Imagining", "5. Having", "6. Getting", "7. Avoiding", "8. Cheaping", "9. Letting"]),
    ("책에 나온 기업 사례들은 각각 어떤 원칙을 적용했나",
     ["The Porsche Story", "LinkedIn—", "Dräger—", "Uber—", "Swarovski—", "Optimizely—", "Innovative Pharma"]),
    ("지불 의사(WTP) 조사 결과는 제품 개발 과정의 어느 단계들에서 쓰이나", ["CHAPTER 4:", "CHAPTER 5:", "CHAPTER 6:", "CHAPTER 9:"]),
]
