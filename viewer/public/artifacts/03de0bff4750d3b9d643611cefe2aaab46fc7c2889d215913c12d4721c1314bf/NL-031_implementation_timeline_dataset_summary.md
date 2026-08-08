---
source_id: "NL-031"
title: "Implementation Timeline Dataset Summary"
document_type: "implementation_export"
status: "export_summary"
publication_date: "2026-07-04"
author_department: "Customer Success"
author_role: "Implementation Lead"
author_name: "Lina Park"
authority_level: 4
intended_audience: "Customer Success, sales engineering, operations management"
related_documents: ["NL-005", "NL-012", "NL-020", "NL-024"]
synthetic: true
---

# Implementation Timeline Dataset Summary

## [NL-031-S01] Dataset Summary

This document provides a summary analysis of the customer onboarding and implementation timelines for NovaLearn AI during the first half of fiscal year 2026, ending June 30, 2026. Compiled by Lina Park, Implementation Lead, this export captures project-level datasets tracking the duration in weeks from contract signature to the official product go-live date. 

The primary goal of this dataset is to provide Customer Success (CS) representatives, sales personnel, and operations managers with historical benchmarks to manage customer expectations and optimize implementation workflows. Onboarding at NovaLearn is structured around a six-phase playbook (NL-005), which includes discovery, corpus inventory, data cleanup, permission mapping, pilot configuration, and final go-live validation. 

Our data indicates that onboarding duration correlates strongly with company size and plan complexity. Starter onboardings are generally shorter due to simplified requirements, while Growth and Enterprise plans require significant time to resolve permission mapping, single sign-on (SSO), data-retention reviews, and legacy document conversions. Below we detail the implementation metrics and blocker frequencies observed during this tracking period.

### Telemetry and Data Collection
Project milestones are recorded by Customer Success Managers (CSMs) in our internal Jira project tracker under the `MIG-ONBOARD` project board. The duration in weeks is calculated using the timestamps of two key transitions: `Ticket Created (Contract Signed)` to `Status: Closed (Go-Live Approved)`. These timestamps are joined with product telemetry logs from our Admin Console to verify that the workspace passed citation accuracy and permission-mapping audits before the closing status was set.

---

## [NL-031-S02] Timeline by Plan

The duration of the implementation process varies significantly depending on the subscription plan purchased by the customer. The table below represents our standard plan-level expectations compared against the actual historical median values recorded in Q2 2026:

| Subscription Plan | Target Timeline | Actual Median Duration | Included Support | Main Driver of Success |
|---|---|---:|---|---|
| Starter | 2 - 4 weeks | 3.5 weeks | Email support | Standardized FAQ formats, clean documentation |
| Growth | 4 - 6 weeks | 5.2 weeks | Priority support | Customer resource availability, clean permission structures |
| Enterprise | 8 - 10 weeks | 8.7 weeks | Dedicated CSM | Executive alignment, pre-cleared security and SSO |

### Plan-Specific Characteristics and Task Lists
* **Starter Plan:** Onboarding is lightweight. Customers typically upload public-facing manuals or standard marketing materials. Since they do not configure complex role-based access controls (RBAC) or integrate with internal file repositories, they go live quickly. Typical tasks include setting up user seats and performing simple PDF uploads in the KnowledgeHub.
* **Growth Plan:** Requires a structured 4-to-6 week process. These mid-market accounts ingest larger corpora (averaging 500 to 2,000 files) and configure basic role permissions. Delays in this tier are usually caused by customer L&D resources being diverted to other projects. Typical tasks include setting up Slack connector integrations and configuring basic domain restrictions.
* **Enterprise Plan:** Requires an intensive 8-to-10 week process. Enterprise accounts require custom data-retention policies, single sign-on (SSO) configuration via SAML/OIDC, and strict permission mapping to replicate their internal folder hierarchies. This demands substantial CSM and engineering hours.

---

## [NL-031-S03] Timeline by Segment

To provide granular visibility, the dataset tracks onboarding metrics for a sample of our key customer accounts. The table below outlines the segment, plan, onboarding duration in weeks, and primary blocker category for each tracked customer:

| Customer Name | Segment | Subscription Plan | Onboarding Duration (Weeks) | Primary Blocker Category |
|---|---|---|---:|---|
| EduVantage Labs | SMB | Starter | 3.2 | None (Smooth Ingestion) |
| Harbor Foods | SMB | Starter | 3.8 | Source Cleanup (Duplicate FAQs) |
| Atlas Logistics | Mid-Market | Growth | 4.2 | Source Cleanup (Stale Docs) |
| Acme Manufacturing | Mid-Market | Growth | 5.4 | Permission Mapping (AD Sync) |
| Meridian Retail Group | Mid-Market | Growth | 6.0 | Permission Mapping / Integrations |
| Northstar Financial | Enterprise | Enterprise | 8.2 | Security / SSO / Retention Review |
| Beacon Insurance | Enterprise | Enterprise | 8.4 | None (Fast Security Clearance) |
| BrightPath Healthcare | Enterprise | Enterprise | 9.5 | OCR / Scanned PDF Compliance |

### Segment Averages Validation
The historical averages derived from this dataset align exactly with the segment-level metrics reported in our Q2 Business Metrics Report (NL-012):
* **SMB Onboarding Average:** `(3.2 + 3.8) / 2 = 3.5 weeks`. SMB implementations are highly streamlined but represent lower expansion potential.
* **Mid-Market Onboarding Average:** `(4.2 + 5.4 + 6.0) / 3 = 5.2 weeks`. Onboardings are manageable but require careful tracking of resource alignment.
* **Enterprise Onboarding Average:** `(8.2 + 8.4 + 9.5) / 3 = 8.7 weeks`. Enterprise onboardings are prolonged, reflecting compliance and file format complexities.

---

## [NL-031-S04] Blocker Categories

Our project management tool tracks specific events that cause onboarding delays of three or more business days. An analysis of the implementation logs highlights three major blocker categories:

### 1. Permission Mapping (45% of delay incidents)
* **Description:** Replicating a customer's role-based access control (RBAC) rules within NovaLearn.
* **Impact:** In mid-market and enterprise segments, customer admins must ensure that employees query TutorChat without seeing information above their clearance level. Synchronizing Active Directory (AD) groups or folder-level permissions frequently results in configuration errors that freeze go-live validation.
* **Technical Root Cause:** Our Python-based permission worker sync script occasionally encounters recursion limits when processing nested group configurations containing circular dependencies. This halts directory mirroring.
* **Examples:** Acme Manufacturing and Meridian Retail Group experienced onboarding extensions of 1.4 to 2.0 weeks due to Active Directory synchronization issues.

### 2. Source Ingestion Cleanup (30% of delay incidents)
* **Description:** Removing outdated, redundant, or corrupted files from the customer's corpus before indexing.
* **Impact:** Customers frequently dump large folders into the KnowledgeHub without reviewing them. This results in TutorChat drawing answers from outdated policy versions. Onboarding is paused while the customer performs a manual document audit.
* **Examples:** Harbor Foods and Atlas Logistics required manual cleanup phases to purge duplicate and stale FAQ files.

### 3. OCR and Scanned PDFs (25% of delay incidents)
* **Description:** Extracting readable text and formatting from image-only PDF policy manuals.
* **Impact:** Legacy manuals must be processed via OCR to support citation grounding. Because our current indexing system lacks advanced OCR capabilities, scanned PDFs return incomplete citations, failing our go-live validation threshold (which requires 85% citation correctness).
* **Technical Root Cause:** The standard PDF parser fails to detect text layers in image-only documents, resulting in empty indexes unless external OCR pre-processing is applied.
* **Examples:** BrightPath Healthcare experienced our longest onboarding time (9.5 weeks) due to the necessity of manual formatting checks on scanned compliance files.

---

## [NL-031-S05] Fastest and Slowest Implementations

### Case Study: BrightPath Healthcare (9.5 Weeks - Slowest)
BrightPath Healthcare represents our most complex enterprise implementation. As a major healthcare provider with 6,500 employees, their compliance requirements are severe.
* **Challenges:** Their source corpus consisted of over 3,000 legacy PDF policy manuals. Approximately 40% of these manuals were poorly scanned images without text layers. Under our AI Reliability Policy (NL-008), TutorChat must ground all healthcare answers in exact source text. The lack of text layers resulted in citation failures.
* **Resolution:** The implementation team had to manually run external OCR tools on these documents before uploading. This manual formatting verification extended the onboarding timeline to 9.5 weeks.

### Case Study: EduVantage Labs (3.2 Weeks - Fastest)
EduVantage Labs, an education startup with 120 employees, completed the fastest onboarding.
* **Success Factors:** Their corpus was small (under 100 documents) and consisted entirely of clean, modern Markdown and DOCX files. They did not require role-based access controls, as all employees shared the same access level. The implementation passed all citation correctness audits on the first pass, allowing a go-live date in week 3.

---

## [NL-031-S06] Recommendations

To improve implementation efficiency and reduce CSM resource consumption, the Customer Success department recommends:

1. **Mandatory Pre-Onboarding Format Audit:** Require Enterprise prospects to submit a sample of 20 core documents for format analysis during the sales discovery phase. If scanned PDFs are detected, the sales team should flag this early, allowing the CS team to adjust the target timeline.
2. **Standardized AD Templates:** Develop standard Active Directory permission mapping templates for customers using Microsoft Azure AD. Providing pre-configured schema configurations will minimize custom sync errors during Phase 4 of onboarding.
3. **Self-Service Cleanup Script:** Provide customer admins with a script that scans their document repository for duplicates and file size anomalies before upload. This will delegate source cleanup tasks to the customer's IT team, freeing up NovaLearn resources.
4. **Azure Active Directory Connector Blueprints:** Create pre-approved deployment blueprints for customers integrating with Microsoft Entra ID. This will speed up Phase 4 permission setups by providing administrators with verified JSON schema configurations.

---

## [NL-031-S07] Data Notes

* **Data Period:** Projects initiated and completed between 2026-01-01 and 2026-06-30.
* **Sample Size:** 8 key accounts representing distinct segments and plans.
* **Data Sources:** Combined reports from Jira Project Management, Zendesk onboarding queues, and KnowledgeHub system configuration logs.
* **Authority Level:** Operational dataset summary (Authority Level 4). Designed to guide internal operations.

### SQL Ingestion Details
For relational databases, this dataset corresponds to the `implementation_timelines` table structure. SQL queries can be run to calculate average durations and identify blocker frequencies:
```sql
SELECT segment, AVG(typical_duration_weeks) 
FROM implementation_timelines 
GROUP BY segment;
```
This enables operational teams to generate reports on onboarding velocities programmatically.

### Additional SQL Performance Auditing Queries

To identify which specific onboarding phases are responsible for the greatest implementation delays, operations analysts can execute detailed SQL queries against the granular phase telemetry tables. The following query calculates the average duration in days for each onboarding phase (such as permission mapping or OCR processing) across all Enterprise customer deployments:
```sql
SELECT phase_name, AVG(duration_days) AS avg_phase_duration 
FROM onboarding_phase_logs 
WHERE plan_tier = 'Enterprise' 
GROUP BY phase_name 
ORDER BY avg_phase_duration DESC;
```
By analyzing the results of this query, Customer Success managers can target engineering resources toward the highest-latency phases. Furthermore, analysts can run correlations between document volume, scanned page counts, and total onboarding duration to verify if the OCR pipeline represents a statistically significant bottleneck:
```sql
SELECT CORR(scanned_page_count, typical_duration_weeks) AS ocr_correlation 
FROM implementation_timelines;
```
These database telemetry checks are integrated into our quarterly operational performance reviews, helping our technical leads adjust resource allocation. This structured data analysis forms the basis of our annual onboarding budget proposals and headcount planning reviews.
