---
source_id: "NL-003"
title: "Current Pricing and Packaging  -  2026"
document_type: "pricing_page"
status: "current"
publication_date: "2026-06-01"
author_department: "Finance"
author_role: "CEO"
author_name: "Maya Patel"
authority_level: 5
intended_audience: "Sales team, customer success, finance, executive leadership"
related_documents: ["NL-001", "NL-004", "NL-009", "NL-012", "NL-016"]
synthetic: true
---

# NovaLearn AI: Current Pricing and Packaging Policy (2026)

## [NL-003-S01] Pricing Overview

This document outlines the official pricing, packaging, and licensing policies for NovaLearn AI, effective June 1, 2026. This schedule is the sole source of truth for all sales proposals, contract renewals, and financial audits. Any deviations from the pricing models, seat allocations, or support definitions outlined herein must be approved in writing by the Chief Executive Officer or Vice President of Finance.

NovaLearn AI licenses its B2B SaaS platform under a subscription model designed to scale with customer size, complexity, and compliance requirements. Our pricing is structured to align cost with the value delivered across different modules, user capacities, and administrative workloads. By offering clear tiers-Starter, Growth, and Enterprise-we accommodate the operational needs of growing businesses while providing enterprise clients with the dedicated infrastructure, security, and integration capabilities they require.

### Infrastructure Cost Drivers
Our subscription pricing is mathematically modeled on the baseline operational costs associated with hosting advanced RAG pipelines. These infrastructure drivers fall into three primary categories:
1. **Computational Inference**: Running multi-turn, grounded conversations via large language models (LLMs) requires high-end GPU resources. The cost scales directly with query volume and prompt size.
2. **Vector Storage and Embedding Generation**: Converting corporate corpora into 1536-dimensional vectors and maintaining isolated database namespaces in PostgreSQL pgvector requires dedicated RAM and disk allocation.
3. **Advanced OCR Pipeline Processing**: Parsing scanned documents and low-contrast PDFs utilizing neural layout analyzers consumes significant compute, requiring specialized processing nodes.

Sales representatives must reference this document during customer discovery and negotiation. All pricing discussions must be grounded in these official rates. Under no circumstances are sales teams authorized to offer pricing based on historical, deprecated, or informal models (such as the 2025 pricing proposal).

---

## [NL-003-S02] Current Plan Table

The table below defines the primary subscription tiers, base monthly fees, seat allocations, support levels, and standard onboarding fees for NovaLearn AI.

| Plan | Monthly price | Included learners | Admin seats | Support | Onboarding fee |
|---|---:|---:|---:|---|---:|
| Starter | $1,200 | 500 | 3 | Email | $2,500 |
| Growth | $3,600 | 2,000 | 10 | Priority | $7,500 |
| Enterprise | From $8,500 | Custom | Custom | Dedicated CSM | From $15,000 |

*Note: All prices are listed in United States Dollars (USD) and exclude applicable sales taxes. Subscriptions are billed monthly or annually, subject to the contract terms outlined in section `[NL-003-S07]`.*

---

## [NL-003-S03] Plan Details

Each subscription plan is packaged with specific feature access and processing capacities, matching the needs of the corresponding customer segment.

* **Starter Plan ($1,200/month)**: Designed for small teams, pilots, or isolated business units. This plan is limited to 500 learners and 3 admin seats. It includes full access to CourseBuilder, TutorChat, and KnowledgeHub. However, Starter accounts do not have access to InsightDash (analytics) or the Admin Console's advanced security features (such as custom SAML SSO and audit logs). The document processing capacity is capped at 1,000 total files or 100,000 parsed pages. The workspace resides on shared cluster resources.
* **Growth Plan ($3,600/month)**: Engineered for mid-market organizations and rapidly expanding companies. This tier supports up to 2,000 learners and 10 admin seats, and includes access to all core modules, including InsightDash and the Admin Console. Growth accounts benefit from SAML SSO integration and standard audit logging. Document storage is expanded to 5,000 files or 500,000 pages. Growth plans also support automated document source connectors for Google Drive and Microsoft SharePoint.
* **Enterprise Plan (Starting at $8,500/month)**: Built for large, highly regulated organizations requiring custom capacity, custom SSO integrations, advanced security, and high-touch support. Learner seats and admin seats are fully customizable and defined in the customer's specific Order Form. Enterprise accounts receive access to advanced audit logs with extended retention, custom data isolation workspaces, and early access to beta modules (such as WorkflowCoach). Document storage starts at 20,000 files with options to expand, and includes a dedicated server environment with guaranteed query latency SLAs.

Under no circumstances should the feature access limits of Starter or Growth plans be bypassed without a formal plan upgrade.

---

## [NL-003-S04] Onboarding Fees

Every new NovaLearn AI subscription requires a one-time, mandatory onboarding and implementation fee. This fee covers the cost of tenant provisioning, metadata indexing setup, customer administrator training, and basic permission mapping.

* **Starter Onboarding ($2,500)**: Involves automated tenant setup, email-based setup support, and access to self-paced onboarding materials. The implementation window is typically 2-3 weeks, during which the customer must upload their documents and manage their own user invitations.
* **Growth Onboarding ($7,500)**: Includes dedicated setup assistance from an Implementation Specialist, up to 10 hours of live training for administrators, and assistance with standard SSO configuration and folder-level permission mapping. Onboarding typically takes 4-6 weeks.
* **Enterprise Onboarding (Starting at $15,000)**: Required for all Enterprise subscriptions. This fee covers a dedicated Implementation Lead (e.g. Lina Park), custom permission integration (mapping Okta/AD groups to document folders), security review compliance validation, and custom OCR parsing setup for scanned legacy files. The typical onboarding window is 8-10 weeks, and go-live is subject to passing citation and accuracy thresholds. The onboarding fee includes up to 40 engineering hours of custom scripting for folder-level access control list (ACL) replication.

Onboarding fees are non-refundable and must be paid in full upon execution of the subscription agreement before provisioning begins.

---

## [NL-003-S05] Add-ons and Overage Rules

To accommodate businesses with fluctuating workloads or special operational requirements, NovaLearn AI offers optional add-ons and enforces overage charges for usage exceeding plan limits.

* **Learner Overage Fees**: If a Starter or Growth customer exceeds their included learner limit, the system will apply a monthly overage charge. Starter accounts are billed at $3.00 per active learner above the 500 limit. Growth accounts are billed at $2.50 per active learner above the 2,000 limit. Enterprise accounts negotiate custom learner pricing, which is detailed in their specific contracts.
  * *Active Learner Definition*: A learner is considered active if they perform at least one search query in TutorChat or log into CourseBuilder at least once during a given calendar month. Active learner counts are reset at 12:00 AM UTC on the first day of each calendar month.
* **Admin Seat Add-ons**: Starter accounts can add up to 2 additional admin seats for $150/month per seat. Growth accounts can add up to 5 additional admin seats for $100/month per seat.
* **Storage and Ingestion Add-ons**: Customers who exceed their document limits can purchase additional storage packs. A storage pack of 1,000 documents or 100,000 pages can be added to any Growth plan for $300/month.
* **Additional Isolated Workspaces**: Growth and Enterprise customers who require separate document spaces for distinct subsidiaries or geographical branches can purchase additional workspaces for $200/month per workspace.
* **Advanced OCR Processing Pack**: Growth customers who need to process large volumes of scanned PDFs can purchase our advanced OCR processing add-on for $500/month, which allocates additional worker queue capacity for document extraction. OCR processing is included in the base Enterprise plan up to a standard volume of 5,000 scanned pages per month, with excess scanned pages billed at $0.10 per page.

All overages are monitored on a monthly basis and will be invoiced in the subsequent billing cycle unless the customer chooses to upgrade to a higher permanent plan.

---

## [NL-003-S06] Support Tiers

Support services are aligned with the customer's subscription plan, ensuring that higher-value contracts receive rapid resolution times. The table below defines our official Support Level Agreements (SLAs) by plan tier:

| SLA Metric | Starter Plan | Growth Plan | Enterprise Plan |
|---|---|---|---|
| **Support Channel** | Email Portal | Email and Live Web Chat | Email, Chat, and Phone |
| **Response SLA** | 2 Business Days | 8 Business Hours | 1 Hour (Severity-1) |
| **Resolution Target** | Best Effort | 24 Business Hours | 4 Hours (Severity-1) |
| **Support Hours** | 9:00 AM - 5:00 PM EST | 8:00 AM - 6:00 PM EST | 24/7/365 for Sev-1 |
| **Dedicated Agent** | Shared Queue | Priority Routing | Dedicated CSM (e.g. Jordan Lee) |
| **Quarterly Business Reviews**| No | Optional Add-on | Included |

### Service Credit Penalties for SLA Breaches
For Enterprise clients, NovaLearn AI guarantees performance uptime and support responsiveness. If the support team fails to meet the 1-hour response SLA for Severity-1 critical incidents (such as complete workspace lockouts or indexing queue failures), the customer is eligible to receive service credits. These credits are calculated as 5% of the customer's monthly recurring subscription fee for each hour of delayed response, capped at a maximum of 50% of the monthly fee per billing cycle. Service credits must be requested in writing within 30 days of the SLA breach and will be applied to the next invoice.

Support tiers are strictly linked to the active subscription plan and cannot be unbundled or downgraded to reduce base subscription fees.

---

## [NL-003-S07] Discount and Contract Rules

NovaLearn AI encourages long-term partnerships by offering discount structures for multi-year commitments and annual billing.

* **Annual Billing Discount**: Customers on Starter or Growth plans who select annual pre-payment receive a 10% discount on their base subscription fees. Annual billing is mandatory for all Enterprise plans unless a custom payment schedule is authorized by the VP of Finance.
* **Multi-Year Discounts**: A 2-year commitment qualifies the customer for a 15% discount on the base subscription fee. A 3-year commitment qualifies the customer for a 20% discount. Multi-year discounts apply only to the base subscription fee and do not apply to onboarding fees, storage add-ons, or overage charges.
* **Payment Terms**: The standard payment term is Net 30 from the invoice date. Payments can be made via credit card, ACH transfer, or bank wire. Invoices unpaid after 45 days are subject to a late fee of 1.5% per month on the outstanding balance.
* **Discount Approval Thresholds**: Sales representatives are not authorized to offer discounts on subscription or onboarding fees without prior management approval. Any discount up to 10% requires approval from the VP of Sales (Marcus Reed). Discounts between 11% and 20% require approval from the CEO (Maya Patel). Any discount exceeding 20% must be approved in writing by the CEO.

### Billing Disputes and Reconciliation Process
In the event that a customer disputes an invoice (e.g., questioning active learner counts or storage overages), the following operational guidelines apply:
1. *Dispute Filing*: The customer must submit a written dispute to `billing@novalearn.ai` within 14 business days of the invoice date.
2. *System Verification*: The finance team will pull the usage logs from the tenant's Admin Console for the billing period to cross-reference active sessions and document volumes.
3. *Resolution Period*: NovaLearn AI commits to reviewing and resolving all billing disputes within 10 business days of filing. During this audit period, the late payment penalty is suspended.
4. *Adjustment Issuance*: If the dispute is validated, a credit note will be applied to the customer's account within the active billing cycle. If the invoice is found correct, the customer must pay the original amount within 5 business days of resolution.

All contracts automatically renew for subsequent 12-month periods unless either party provides written notice of non-renewal at least 30 days prior to the expiration of the current term.

---

## [NL-003-S08] Superseded Pricing Notes

This document (effective June 1, 2026) fully supersedes all previous pricing schedules, packaging guidelines, and promotional pricing models. Most notably, this document replaces the deprecated 2025 pricing proposal (found in `NL-004_deprecated_pricing_proposal_2025.md`), which proposed a Starter plan at $900/month and a Growth plan at $2,800/month with lower onboarding fees.

Those historical rates were based on early operating cost estimates and have been retired due to the increased compute and storage costs associated with our advanced RAG pipeline, vector database scaling, and OCR services. Sales and customer success teams must ensure that no active prospects are quoted the deprecated 2025 rates. Any contracts utilizing the historical 2025 rates must be migrated to the current 2026 schedule upon their next renewal cycle. Customers under historical contracts will be permitted to run out their current active term, but must transition to the 2026 rates upon contract renewal.

### Grace Period and Transition Procedures

To ensure customer retention during the migration from historical 2025 rates to the updated 2026 pricing schedule, the Customer Success team may offer a one-time, 90-day grace period. During this grace period, qualifying accounts will be billed at a mid-tier rate (for instance, $3,100 per month for Growth, instead of the full $3,600). This grace period must be requested by the dedicated Customer Success Manager (CSM) and approved by the VP of Customer Success. The transition procedure requires the finance team to audit the client's historical seat usage, storage footprint, and average query volume over the preceding six months. If a client's usage has consistently exceeded the legacy boundaries, they must transition directly to the 2026 rates without a grace period. All transition agreements must be signed by the customer at least 15 days prior to their contract renewal date to avoid automatic suspension of their workspace access.
