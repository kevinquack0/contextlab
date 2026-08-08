---
source_id: "NL-004"
title: "Deprecated Pricing Proposal  -  2025"
document_type: "pricing_page"
status: "deprecated"
publication_date: "2025-10-30"
author_department: "Finance"
author_role: "Finance Manager"
author_name: "Lilian Zhao"
authority_level: 1
intended_audience: "Executive leadership, sales management"
related_documents: ["NL-001", "NL-003"]
synthetic: true
---

# NovaLearn AI: Deprecated Pricing and Packaging Proposal (2025)

## [NL-004-S01] Deprecated Notice

**CRITICAL WARNING: THIS DOCUMENT IS FULLY DEPRECATED AND RETIRED. IT DOES NOT REPRESENT CURRENT NOVALEARN AI PRICING OR SERVICES.**

This document, originally drafted and published on October 30, 2025, outlines a proposed pricing and packaging model that was evaluated by the executive leadership team for the subsequent fiscal year. Following comprehensive financial audits, infrastructure cost analyses, and sales pilot results, this proposal was rejected and subsequently archived. The company security and finance governance mandates require that all historical financial proposals be clearly marked as deprecated to prevent commercial errors in active sales channels.

This file is preserved solely for historical context, data auditing, and pricing model comparisons. Sales representatives, customer success managers, and marketing teams are strictly prohibited from quoting, referencing, or displaying these rates to any active prospects, customers, or partners. All current quotes and contract negotiations must be based on the official 2026 pricing schedule, which is documented in [NL-003_current_pricing_packaging_2026.md](novalearn_synthetic_corpus/corpus/01_official_company_product/NL-003_current_pricing_packaging_2026.md).

---

## [NL-004-S02] Historical Pricing Proposal

The table below details the pricing structures, user limits, and implementation fees that were proposed in late 2025 but never fully adopted as our long-term pricing model.

| Plan | Monthly price | Included learners | Admin seats | Support | Onboarding fee |
|---|---:|---:|---:|---|---:|
| Starter | $900 | 500 | 3 | Email | $1,500 |
| Growth | $2,800 | 2,000 | 10 | Priority | $4,500 |
| Enterprise | From $6,000 | Custom | Custom | Dedicated CSM | From $10,000 |

*Note: The rates listed above represent early draft estimates. These rates are fully obsolete and have been superseded by the active 2026 pricing schedule. Do not use these figures in any active commercial agreements. Doing so violates company financial policy and will result in invoice rejection.*

---

## [NL-004-S03] Old Plan Assumptions

The historical proposal of 2025 was structured around several early assumptions regarding system usage, data storage, and compute costs. Under this proposed framework, the features were packaged as follows:

* **Starter Tier ($900/month)**: This plan was designed to target small technology startups and growing businesses. The assumptions were that Starter users would primarily ingest flat text files (Markdown, raw text) and would have very low query volumes in TutorChat (under 50 queries per user per month). It was assumed that simple email-based support would require less than 2 hours of engineering attention per customer per month, and that standard onboarding could be fully automated with a $1,500 setup fee. Additionally, it assumed shared database clusters without need for custom logical namespace routing.
* **Growth Tier ($2,800/month)**: This plan targeted mid-market companies with up to 2,000 learners. The team assumed that mid-market clients would upload structured files such as Word documents and PDFs, but would not require extensive permission mappings or custom security configurations. It was assumed that priority support could be handled within our existing support queue without hiring dedicated support engineers, and that an onboarding fee of $4,500 would fully cover the manual configuration of SSO.
* **Enterprise Tier (Starting at $6,000/month)**: This tier was designed to offer custom capacities for larger clients. It assumed that an entry-level price of $6,000 would cover the storage costs of up to 10,000 documents and that dedicated CSM support could be shared across 15 to 20 enterprise clients. The billing team assumed that custom data processing agreements (DPAs) would require minimal legal review and that data deletion could be executed instantly upon contract termination without technical complexity.

These assumptions did not account for the high processing overhead of scanned PDFs, the engineering complexity of mapping legacy Active Directory permissions, or the rising API and compute costs associated with running multi-turn grounded conversations.

---

## [NL-004-S04] Why This Proposal Was Replaced

In early 2026, the finance and engineering teams completed a joint cost analysis that revealed this 2025 proposal was financially unsustainable. Several key factors led to the rejection and replacement of this pricing model:

1. **Underestimated Infrastructure Costs**: The computational resources required to run semantic searches, generate vector embeddings, and process grounded responses via large language models (LLMs) were significantly higher than expected. The Starter plan at $900/month would have operated on negative margins for customers with high query volumes.
2. **The Scanned PDF and OCR Bottleneck**: A major portion of customer files uploaded into KnowledgeHub consisted of scanned, low-quality PDFs. Processing these files required running heavy OCR models, which increased compute costs by over 300% for some accounts. The proposed onboarding fees ($1,500 for Starter and $4,500 for Growth) did not cover the engineering hours required to clean and format this data.
3. **Permission Mapping Complexity**: The assumption that mid-market and enterprise clients could onboard with minimal support was incorrect. Mapping complex, nested folder permissions from Okta or Active Directory into KnowledgeHub required extensive manual configuration, extending onboarding timelines and consuming significant Customer Success resources.
4. **Support Escalations**: Email support on the Starter plan and priority support on the Growth plan both required more engineering time than originally estimated. Customers frequently encountered citation issues, requiring support operations to manually debug vector chunks.

### Financial Variance Audit Details
To analyze the financial variance, Lilian Zhao compiled a comparison table showing the projected infrastructure costs per tenant versus the actual expenses recorded during the Q1 2026 pilot program:

| Infrastructure Cost Category | Projected Cost / Tenant | Actual Q1 2026 Cost | Budget Variance (%) | Primary Cost Driver |
|---|---|---|---|---|
| **LLM Inference API** | $120.00 / month | $380.00 / month | +216.7% | High token counts per grounded query |
| **Vector DB Storage & Index** | $45.00 / month | $110.00 / month | +144.4% | Nested document chunking redundancy |
| **Ingestion and OCR Compute** | $30.00 / month | $185.00 / month | +516.7% | Unanticipated volume of scanned PDFs |
| **Directory Sync & RBAC API**| $15.00 / month | $65.00 / month | +333.3% | Nested Active Directory mapping calls |
| **Technical Support Engineering**| $80.00 / month | $240.00 / month | +200.0% | Debugging broken citations in OCR files|
| **Total Operational Cost** | **$290.00 / month** | **$980.00 / month** | **+237.9%** | **Compute scaling inefficiency** |

### Case Studies of Pilot Failures Under 2025 Pricing
Three specific customer pilots in Q1 2026 proved that the 2025 proposal was operationally and financially unviable:
* **Acme Manufacturing Pilot**: During their trial, Acme uploaded approximately 800 legacy safety manuals, over 70% of which were scanned, low-contrast PDFs. The ingestion queue experienced three separate worker crashes due to memory timeouts during OCR extraction. Resolving these issues required over 28 hours of custom support engineering, which far exceeded the proposed $4,500 Growth onboarding fee.
* **Atlas Logistics Pilot**: Atlas uploaded 450 documents, but the corpus contained severe content duplication and outdated SOPs from 2022. This led to TutorChat retrieving conflicting answers and hallucinating citations in 14% of test queries. The customer success team had to spend 15 hours performing manual corpus cleanup, showing that automated onboarding assumptions were incorrect.
* **BrightPath Healthcare Pilot**: As a highly regulated enterprise, BrightPath required mapping Okta user groups to a complex 12-level nested document folder structure in KnowledgeHub. Establishing and validating this secure permission schema required 80 hours of senior implementation engineering work, making the proposed $10,000 enterprise onboarding fee completely unprofitable.

This audit made it clear that the $900/month Starter plan, which was designed to support up to 500 learners, was operating at an operational loss once query volumes scaled. To protect company margins and ensure we could deliver the high-quality support and security required by our clients, the pricing model was revised. The base monthly rates were increased, and onboarding fees were restructured to accurately reflect the implementation labor required for each tier.

---

## [NL-004-S05] Reference to Current Pricing

All current clients, renewals, and new sales negotiations must refer exclusively to the 2026 pricing schedule. The active pricing, packaging limits, and onboarding requirements are fully detailed in [NL-003_current_pricing_packaging_2026.md](novalearn_synthetic_corpus/corpus/01_official_company_product/NL-003_current_pricing_packaging_2026.md).

For comparison, the current pricing represents the following adjustments over the deprecated 2025 proposal:
* **Starter Plan**: Increased from $900/month to $1,200/month. Onboarding fee adjusted from $1,500 to $2,500 to cover automated ingestion resources.
* **Growth Plan**: Increased from $2,800/month to $3,600/month. Onboarding fee adjusted from $4,500 to $7,500 to account for dedicated implementation specialist support and SSO setup.
* **Enterprise Plan**: Starting rate increased from $6,000/month to $8,500/month. Onboarding fee adjusted from $10,000 to a minimum of $15,000 to cover custom Okta/AD permission mapping and advanced OCR pipeline setup.

By establishing these sustainable rates, NovaLearn AI has been able to invest heavily in platform reliability, security audits, and the development of advanced capabilities, including our upcoming WorkflowCoach module. Please archive this document and ensure all sales materials are updated to reflect the current 2026 schedule. Any legacy contracts utilizing these deprecated 2025 rates must be migrated to the current 2026 schedule upon their next renewal cycle. Customers under historical contracts will be permitted to run out their current active term, but must transition to the 2026 rates upon contract renewal.

### Sales Enablement Collateral Audit and Deletion Guidelines

To ensure that outdated marketing materials do not lead to compliance issues, the sales enablement team conducts quarterly audits of all shared Google Drive and SharePoint folders. Any PDF brochure, slide deck, or proposal sheet found containing the historical 2025 rates must be flagged and permanently deleted within 5 business days. Sales operations will distribute updated templates featuring the current 2026 pricing schedule to all active personnel. In addition, our internal intranet search index has been configured to demote or filter out legacy document files, ensuring that employees searching for pricing guidelines are directed exclusively to the active policy. If a sales representative identifies a client contract that is still billing under the obsolete 2025 rates, they must notify the finance department immediately. Finance will then prepare a transition proposal for the client's upcoming renewal window, ensuring a smooth transition to the sustainable 2026 tiers.
