---
source_id: "NL-019"
title: "Customer Interview Transcript  -  Northstar Financial"
document_type: "interview_transcript"
status: "transcript"
publication_date: "2026-05-03"
author_department: "Customer Success"
author_role: "Head of Customer Success"
author_name: "Jordan Lee"
authority_level: 3
intended_audience: "Security team, legal department, customer success, product management"
related_documents: ["NL-007", "NL-011"]
synthetic: true
---

# Customer Interview Transcript  -  Northstar Financial

## [NL-019-S01] Interview Metadata

*   **Customer Entity:** Northstar Financial
*   **Customer Segment:** Enterprise (Financial Services)
*   **Active Subscription Plan:** Enterprise Plan
*   **Interview Date:** May 3, 2026
*   **Interview Duration:** 45 minutes
*   **Interviewer(s):**
    *   Jordan Lee, Head of Customer Success (NovaLearn AI)
    *   Priya Raman, Security and Governance Lead (NovaLearn AI)
*   **Interviewee(s):**
    *   Robert Vance, VP of Information Security (Northstar Financial)
    *   Diane Sterling, Procurement Manager (Northstar Financial)
*   **Primary Topic:** Security Audit, Log Retention, and Data Deletion Timelines
*   **Document Status:** Approved Transcript (Internal CS Repository)

---

## [NL-019-S02] Customer Context

Northstar Financial is a regional retail banking and investment services firm with 4,900 employees across 140 branches and administrative offices. Northstar Financial purchased NovaLearn's Enterprise Plan in March 2026 and is currently in the initial sandboxed implementation phase. Their primary objective is to equip financial advisors and compliance teams with TutorChat to answer queries on mortgage lending regulations, investment compliance, and internal credit policies. Given the regulatory oversight by the SEC and FINRA, Northstar requires a thorough security audit of NovaLearn's infrastructure, logical data separation, audit logging capabilities, and data retention policies before importing production financial policies.

---

## [NL-019-S03] Transcript  -  Security Review

**Jordan Lee [00:01:30]:** Good afternoon, Robert, Diane. Thank you for taking the time to meet with Priya and me today. Today we want to follow up on your security team's review of NovaLearn, address your questions about audit logging, and clarify the data retention and deletion timelines. Priya is here to walk through our technical architecture and address any specific compliance queries. Robert, let's start with your team's feedback on the security review.

**Robert Vance [00:02:00]:** Thanks, Jordan. Let me start by saying that from a functional perspective, our compliance team is very impressed with TutorChat. The ability to search our internal credit policies and get instant citations is exactly what we need. However, because we are a financial institution, our data governance requirements are extremely strict. We are subject to regular audits by the SEC and FINRA, and we must comply with GLBA regulations. This means we must show that we have complete control over where our data is stored, how it is accessed, who has authorization to view it, and how it is destroyed once our contract ends.

Our security team completed their initial analysis of your workspace isolation architecture. We see that you use logical separation between customer tenants in your database and vector search indexes. Priya, can you confirm if there is any shared caching or cross-tenant data indexing occurring in the background? We cannot afford to have any leaks, even in memory.

**Priya Raman [00:03:15]:** I can confirm that there is no shared caching or cross-tenant indexing. Under our Security and Data Governance Policy (`NL-007`), each customer workspace is logically isolated. The database tables, document chunks, and vector embeddings are segregated using unique tenant IDs. Every query submitted by a user is authenticated and scoped to their specific workspace.

Specifically, we use PostgreSQL Row-Level Security (RLS) policies on our relational tables. RLS enforces isolation by binding session variables to the tenant ID of the authenticated user, making it impossible for queries to return records from another tenant. The vector search indexes, which run on our vector database, segregate customer data using isolated metadata namespaces. When a user submits a query to TutorChat, the search is mathematically restricted to their tenant's namespace.

All API calls to the LLM are encrypted using TLS 1.3, and data-at-rest is encrypted using AES-256. We support integration with AWS Key Management Service (KMS), allowing enterprise clients to use their own customer-managed keys (CMK) for data encryption. These keys can be rotated every 90 days according to your internal security protocols. There is absolutely no crossover of data, and no other customer can access your documents or vector space.

**Robert Vance [00:04:00]:** That aligns with what our security team saw in your documentation. What about the model itself? We need to make sure that our proprietary documents and user queries are not used to train or fine-tune NovaLearn's base models. If our internal credit underwriting strategies or mortgage rates were to leak into a shared model, that would be a major compliance breach and a competitive risk.

**Priya Raman [00:04:30]:** We do not use customer data, document uploads, or user queries to train our base models. The models are pre-trained and run in inference-only mode for customer workspaces. Your data remains strictly within your isolated tenant storage. We can add this language explicitly to the custom Data Processing Addendum (DPA) we are drafting for Northstar to ensure your legal team has it in writing.

---

## [NL-019-S04] Transcript  -  Audit Logs

**Robert Vance [00:05:15]:** That's good. Now let's move on to audit logging. Our security team needs to audit all administrative actions within the NovaLearn platform. Specifically, we need to track:
1.  Who uploaded or deleted a document in the KnowledgeHub.
2.  Any changes to folder-level permissions.
3.  Any changes to user roles (e.g., promoting a user to admin).
4.  All user search queries and the corresponding answers.

We see that the Enterprise Plan includes audit logging. What is the standard retention window for these logs, and how do we access them?

**Priya Raman [00:06:00]:** Under our security policy (`NL-007-S05`), audit logs are available for all Growth and Enterprise workspaces. For Growth customers, the logs are retained in the Admin Console for 180 days. For Enterprise customers, we offer an extended retention window of up to 365 days by default, and we can configure longer retention schedules upon request. Admins can view these logs directly in the Admin Console or export them as JSON.

We support four primary user roles: "SuperAdmin" (full workspace and user controls), "WorkspaceAdmin" (manages documents and permissions), "ContentEditor" (can upload and edit files in the KnowledgeHub), and "ActiveLearner" (accesses TutorChat and CourseBuilder lessons). Any changes to these user roles, such as promoting a ContentEditor to WorkspaceAdmin, are recorded as audit events.

**Robert Vance [00:06:45]:** A 365-day retention is sufficient for our standard reviews, but for compliance investigations, we often need logs that go back much further to satisfy regulatory audits. We would like to set up an automated feed to push these audit logs daily to our internal SIEM system, which is Splunk. That way, we store the logs permanently in our own secure archive. Does your API support automated audit log extraction?

We want to write specific threat detection rules in Splunk. For instance, our security operations center (SOC) alerts us if any user downloads more than 50 pages of documentation in under 10 minutes, or if an advisor queries credit compliance guidelines outside of their designated branch region. We need the logs to feed Splunk automatically.

**Priya Raman [00:07:15]:** Yes, we provide an administrative API endpoint specifically for audit log extraction: `/api/v1/admin/audit-logs`. Enterprise customers can use this endpoint to schedule daily exports of their event logs. The endpoint supports pagination, filtering by event type, and date range parameters. The API is authenticated via Bearer tokens, and we can restrict access using IP whitelisting so that only your Splunk server's IP address can query the endpoint.

The payload is returned as a structured JSON object containing timestamp, event ID, user role, workspace ID, query tokens, response tokens, and an array of citation references. We will provide your IT team with the API documentation and sample authentication headers next week to help set this up during the pilot phase.

**Robert Vance [00:07:45]:** That is exactly what we need. Our security team runs periodic penetration testing and log audits, so being able to ingest this data directly into Splunk will allow us to monitor the sandbox pilot in real-time.

---

## [NL-019-S05] Transcript  -  Data Retention Questions

**Diane Sterling [00:08:00]:** That is helpful, Priya. I want to jump in here on the commercial and legal side of data retention, specifically regarding contract termination. During the sales process, our sales rep, Marcus Reed, mentioned that if we terminate the contract, all our data is deleted immediately from your systems.

However, when I reviewed your official Security and Data Governance Policy (`NL-007-S06`), it says that standard data deletion occurs within 90 days after contract termination. This is a significant discrepancy. In our industry, we need clear timelines. If the sales rep says immediate, but the policy says 90 days, which is it? We need a clear commitment because our compliance department cannot sign off on conflicting statements.

**Jordan Lee [00:09:10]:** I appreciate you bringing that up, Diane. This is a common point of confusion. The sales playbook (`NL-009`) sometimes simplifies security policies to address buyer questions quickly, but our official Security and Data Governance Policy (`NL-007`) is the source of truth.

Under our standard policy, when a contract is terminated or a workspace is deleted, the data is marked for deletion. It is immediately removed from active user access and TutorChat queries. However, the physical deletion of database records and document chunks from our primary storage and secure backups takes up to 90 days to complete. This timeline ensures we can securely purge all redundant copies from our backup systems without risking data corruption.

**Robert Vance [00:10:30]:** Why does it take 90 days? In financial services, if we terminate an agreement, we prefer that our proprietary credit underwriting data is purged much faster to minimize exposure. Can we request a custom deletion SLA under our Enterprise agreement?

**Priya Raman [00:11:00]:** The 90-day window is our standard SLA to ensure thorough deletion across all backup servers. Under `NL-007-S06`, we do allow policy exceptions for Enterprise clients. If Northstar requires a faster deletion window, we can negotiate a custom SLA in our DPA.

For example, we can commit to purging all active database records within 30 days and backup archives within 60 days. Our backup rotation cycle consists of daily incremental backups (retained for 30 days) and weekly full backups (retained for 60 days). These backups are stored in Amazon S3 buckets with strict lifecycle policies that enforce automatic deletion.

Purging backup archives faster than 60 days is technically impossible because it would disrupt our backup integrity. To support the 30-day active and 60-day backup purge, we execute specialized script-based deletes that target your tenant ID in backup volumes during rotation cycles. If a service provider claims they delete backups instantly, they are likely not running robust backup cycles.

**Robert Vance [00:11:45]:** That makes technical sense. A 30-day active purge and 60-day backup purge is acceptable. As long as we have that documented in the legal DPA, my security team will approve the exception. We just need to make sure we don't rely on verbal or informal commitments from the sales team.

---

## [NL-019-S06] Transcript  -  Procurement Concerns

**Diane Sterling [00:12:30]:** That is reasonable. Now, let's talk about the onboarding timeline and some of the pricing and billing details. We are currently in week three of our sandboxed pilot. We were planning for a 4-week onboarding, but our implementation coordinator, Lina, mentioned that Enterprise onboarding usually takes 8-10 weeks. Why is the timeline so long, and what can we do to accelerate it?

**Jordan Lee [00:13:00]:** The typical onboarding timeline for Enterprise customers is 8-10 weeks, as detailed in our Onboarding Playbook (`NL-005`). This timeline is not due to technical setup, which can be done in a few days. The delays are almost always related to permission mapping and security reviews.

For example, we need to map your active directory groups to NovaLearn's workspace permissions. Since Northstar has complex role-based access requirements, we need to verify that advisors in the retail banking workspace cannot search or retrieve compliance manuals from the wealth management workspace. Testing these permission boundaries takes time. 

Additionally, if we encounter scanned PDFs that lack text layers, we must process them through OCR before they can be indexed. That can add weeks to the timeline if the document library is large. We have noticed that your mortgage credit policy library contains about 1,400 legacy documents, many of which are scanned.

**Diane Sterling [00:13:30]:** And what about pricing limits? Are there overage charges for query volumes or ingestion pipeline capacity?

**Jordan Lee [00:13:50]:** Since you are on the Enterprise Plan, there are no standard overage fees for query volumes. You have unlimited queries. However, we do cap the number of concurrent document ingestion pipelines at 5 to ensure queue stability. If you import massive files in bulk, they will queue. We also include custom onboarding support in your ACV to cover permission mapping.

**Diane Sterling [00:14:05]:** That sounds fair. We prefer annual invoicing, and our procurement department requires net-45 payment terms. We will need to specify these terms in our Master Services Agreement (MSA).

**Jordan Lee [00:14:10]:** Yes, net-45 payment terms are standard for our Enterprise tier. We will align with our sales team to ensure the MSA reflects this payment structure correctly.

**Diane Sterling [00:14:20]:** Excellent, that will satisfy our vendor management team. We will also need your SOC 2 Type II compliance report for our annual vendor risk assessment review.

**Jordan Lee [00:14:30]:** We can certainly provide that, Diane. I will email the SOC 2 report along with our latest security whitepaper to your team this afternoon.

**Robert Vance [00:14:40]:** We do have a large number of scanned documents. I'll make sure our IT team coordinates with Lina to identify these files early and begins structuring our Active Directory roles to map directly to NovaLearn workspaces. We'll start this grouping now to avoid delays during the production phase.

**Jordan Lee [00:14:45]:** That will help accelerate the process, Robert. We will work closely with your team to streamline these steps.

---

## [NL-019-S07] Interviewer Notes

*   **Key Security Requirements:**
    *   Logical workspace isolation and no customer data usage for model training.
    *   API endpoints for daily audit log extraction to their SIEM (Splunk).
    *   Custom DPA terms: 30-day active database purge and 60-day backup purge post-termination.
*   **Identified Risks:**
    *   Misalignment between sales claims (immediate deletion) and official policy (90 days).
    *   Potential onboarding delays due to scanned PDFs and complex permission mapping.
*   **Customer Health:** Medium-High. The customer is highly engaged and has clear, reasonable security requirements. Resolving the data deletion SLA in the DPA is critical to moving forward.

---

## [NL-019-S08] Follow-Up Actions

### 1. Draft Custom DPA Deletion SLA (Priya Raman)
*   **Action:** Draft the custom deletion SLA (30 days active / 60 days backup) and coordinate with the legal department to include it in Northstar's DPA.
*   **Owner:** Priya Raman
*   **Target Date:** May 10, 2026

### 2. Provide Audit Log API Documentation (Priya Raman)
*   **Action:** Send the administrative API documentation for audit log extraction to Robert Vance's IT security team.
*   **Owner:** Priya Raman
*   **Target Date:** May 6, 2026

### 3. Schedule Onboarding Acceleration Sync (CS / Implementation)
*   **Action:** Schedule a meeting with Lina Park and Northstar's IT leads to review their document folders and identify scanned PDFs that require OCR.
*   **Owner:** Jordan Lee
*   **Target Date:** May 8, 2026
