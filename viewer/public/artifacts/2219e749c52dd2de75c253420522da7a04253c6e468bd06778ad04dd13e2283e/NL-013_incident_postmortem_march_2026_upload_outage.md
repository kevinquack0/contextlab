---
source_id: "NL-013"
title: "Incident Postmortem  -  March 2026 Upload Outage"
document_type: "postmortem"
status: "current"
publication_date: "2026-03-18"
author_department: "Engineering"
author_role: "CTO"
author_name: "Leon Grayson"
authority_level: 4
intended_audience: "Engineering, product support, leadership"
related_documents: ["NL-007", "NL-008", "NL-022"]
synthetic: true
---

# Incident Postmortem  -  March 2026 Upload Outage

## [NL-013-S01] Incident Summary

On March 14, 2026, NovaLearn AI experienced a localized service degradation affecting our document ingestion and indexing pipeline. The incident began at 14:02 UTC and was fully resolved by 15:28 UTC, representing a total service interruption duration of 86 minutes. 

During this window, our document import worker pool became unresponsive, preventing customers from uploading new documents, editing existing document metadata, or completing the indexing required for TutorChat citations. The core web application and search functions remained active; however, any document processing task remained stuck in a "pending" state. 

The incident was triggered by a database schema migration scheduled during a minor configuration update. This migration resulted in a retry storm from our background workers, quickly exhausting the available connection pool and starving our Redis-backed indexing queue. Normal operations were restored after the engineering team rolled back the schema configuration change, flushed the retry queue, and restarted the worker pool with capped retry limits. 

### Service Level Agreements (SLA) Context
Under our standard enterprise terms, NovaLearn AI commits to a 99.9% uptime for core querying and retrieval services. While this SLA was technically maintained because TutorChat remained available to answer queries from previously indexed documentation, the ingestion pipeline outage breached our operational target of processing uploaded documents within 10 minutes. This outage highlights the interdependence of our write and read paths and requires a structural remediation of our database pooling architecture.

---

## [NL-013-S02] Impact

The incident directly impacted a subset of our customer base. Our system logging confirms that exactly 17 active customer accounts were affected by the delayed document imports during this 86-minute window. 

### Customer Impact Details
* **Document Import Failures:** A total of 142 document upload requests failed or remained stuck in the indexing queue. Users saw a persistent "processing" spinner in the KnowledgeHub admin dashboard.
* **Delayed Citation Availability:** For documents that were partially uploaded just prior to the incident, citation indexing was delayed. Employees querying TutorChat could not retrieve answers citing these newly uploaded files, as the database records linking text blocks to their source segments were unavailable.
* **No Impact on Core Querying:** The TutorChat query engine itself remained fully operational. Users could search and chat against previously indexed files without any performance degradation. No data loss occurred, and all pending documents were successfully processed after resolution.

While some internal engineering standup notes informally estimated that "maybe around 20 customers" were affected (as noted in NL-022), this official postmortem confirms through database transaction log audits that the count of affected customer workspaces is exactly 17.

### Affected Customers and Upload Volumetrics
To assist Customer Success in client outreach, the database audit identified the specific workloads queued during the outage:

| Customer Workspace | Plan | Segment | Pending Documents | Max Latency (Min) | Secondary Downstream Effects |
|---|---|---|---:|---:|---|
| **BrightPath Healthcare** | Enterprise | Enterprise | 45 | 84 | High-volume batch import blocked, CS review stalled |
| **Acme Manufacturing** | Growth | Mid-Market | 12 | 81 | Frontline SOP updates delayed, floor training paused |
| **Northstar Financial** | Enterprise | Enterprise | 15 | 78 | Security policy ingest queued, compliance check delayed |
| **Atlas Logistics** | Growth | Mid-Market | 8 | 76 | Driver handbook upload stalled, onboarding delayed |
| **Meridian Retail Group** | Growth | Mid-Market | 18 | 72 | Product catalog ingest delayed, ticket volume spike |
| **EduVantage Labs** | Starter | SMB | 4 | 68 | Demo environment failed to update, sales demo delayed |
| **Harbor Foods** | Starter | SMB | 2 | 60 | Basic FAQ adjustments queued, minor admin friction |
| *10 Other Accounts* | Various | Various | 38 | 86 | Minor administrative delay in updating knowledge bases |
| **Total** | -- | -- | **142** | **86** | General support queue volume increase |

---

## [NL-013-S03] Timeline

All times in the timeline below are recorded in UTC on March 14, 2026:

* **13:50 UTC** - Deployment script initiated for release version `v2.4.12-patch1`. The deploy is flagged as low-risk and includes metadata schema adjustments.
* **13:55 UTC** - Engineering starts deploying the planned configuration change to the document ingestion microservice. The deploy includes a schema migration to support granular document-level permission tags.
* **14:02 UTC** - Deployment completes. Database CPU spikes to 98%. Import worker health checks begin failing as workers time out waiting for database connections.
* **14:05 UTC** - Network firewall logs show a sudden spike in internal HTTP 504 Gateway Timeouts between the backend API and the document worker pool.
* **14:08 UTC** - Automated alert triggers: `indexing-queue-latency-high`. On-call engineer responds and opens investigation.
* **14:12 UTC** - On-call engineer logs into the Datadog dashboard and notices database pool utilization at 100%. All 150 available PostgreSQL connection slots are occupied.
* **14:15 UTC** - Customer Support receives the first ticket regarding stuck uploads. Customer success teams are notified.
* **14:18 UTC** - On-call engineer identifies that the bulk of the connections are held by `import_worker` processes executing the query `SELECT * FROM document_metadata FOR UPDATE`.
* **14:20 UTC** - Engineering on-call engineer opens an incident response bridge on Zoom. The CTO (Leon Grayson) and VP Product (Sofia Almeida) join.
* **14:22 UTC** - Support notes multiple reports of "document upload stuck on processing." The engineering incident commander escalates the issue to a Sev-2 Incident.
* **14:28 UTC** - Database administrator (DBA) identifies an `AccessExclusiveLock` on the `document_metadata` table, blocked by an uncommitted transaction from the schema migration script.
* **14:35 UTC** - Engineering isolates the root cause: a schema migration during the deployment had locked the `document_metadata` table, causing import workers to fail. Due to an uncapped retry policy, the workers initiated a retry storm, generating 12,000 requests per minute and exhausting the database connection pool.
* **14:42 UTC** - Incident bridge discusses options: attempting to manually kill the locking migration transaction vs rolling back the entire patch. DBA warns that killing the transaction mid-migration could leave the database in an inconsistent state.
* **14:50 UTC** - Incident commander decides to roll back the configuration deploy to restore connection pool stability.
* **14:55 UTC** - Rollback script initiated. Database traffic is temporarily throttled to prevent queries from crashing the database.
* **15:05 UTC** - Rollback of the configuration deploy completes. Database CPU drops back to 15%. However, the background Redis queue remains clogged with 85,000 queued retry tasks.
* **15:08 UTC** - Import workers restarted, but they immediately saturate the database again due to the massive backlog of retry tasks in the Redis queue. Database CPU spikes back to 90%.
* **15:12 UTC** - Engineering executes a targeted flush of the retry queue, purging the retry storm messages while preserving the original pending document upload tasks.
* **15:18 UTC** - Database connection pool stabilized. DBA increases connection limits to 200 temporarily to speed up backlog processing.
* **15:20 UTC** - Import workers restarted. Document processing resumes, and the queue backlog begins clearing.
* **15:25 UTC** - Status logs show queue depth decreasing by 1,000 documents per minute.
* **15:28 UTC** - All backlog documents are successfully processed and indexed. Health checks return to green. Incident is declared resolved.
* **15:45 UTC** - Post-incident cleanup completed; database connection limit returned to default 150.

---

## [NL-013-S04] Root Cause

The root cause of the incident was a combination of an uncoordinated database schema migration and an uncapped exponential backoff retry policy in our document ingestion workers.

### Technical Breakdown

#### 1. Schema Locking Mechanics
The deployment introduced a database migration that added a foreign key constraint to the `document_metadata` table to support the upcoming release of our granular permission-tagging feature.
```sql
ALTER TABLE document_metadata 
ADD CONSTRAINT fk_document_permissions 
FOREIGN KEY (permission_tag_id) REFERENCES permission_tags(id);
```
Because the `document_metadata` table contained millions of rows of indexed text snippets, PostgreSQL had to scan the entire table to validate the constraint. During this scan, PostgreSQL acquired an `AccessExclusiveLock` on the table. In PostgreSQL's concurrency control model, an `AccessExclusiveLock` is the most restrictive lock level. It conflicts with all other lock modes, including the `AccessShareLock` acquired by standard `SELECT` queries and the `RowShareLock` acquired by `INSERT` or `UPDATE` transactions. As a result, every other query attempting to touch the `document_metadata` table was forced into a lock queue, waiting for the migration transaction to release the table.

#### 2. Worker Retry Storm
While the table was locked, incoming document import workers failed to write metadata and immediately threw connection timeout errors. Our background worker configuration, managed by Celery on Redis, lacked a proper jitter and retry limit. Instead, it was configured with a default setting of:
```python
@app.task(bind=True, max_retries=50, default_retry_delay=1)
def process_document_import(self, doc_id):
    # Ingestion code here
```
This configuration caused failed jobs to retry immediately after 1 second up to 50 times. Within 10 minutes of the lock, the 17 active customers uploading files generated 142 distinct tasks, which translated into a recursive retry storm generating 12,000 requests per minute.

#### 3. Database Connection Starvation
The retry loops saturated the PostgreSQL connection pool. Since our web application (which runs TutorChat) shares the same PostgreSQL instance as the ingestion worker microservice, the exhaustion of the connection pool began starving the web application. When pgBouncer connection queues filled up, the client requests were dropped, causing slow query responses and frontend gateway timeouts.

---

## [NL-013-S05] Detection and Response

The incident was successfully detected through our automated Prometheus monitoring system. The alert `indexing-queue-latency-high` fired within 6 minutes of the initial deployment completion, notifying the on-call engineer at 14:08 UTC.

### Telemetry Details
The automated alert was triggered by the following Prometheus query:
```prometheus
sum(queue_latency_seconds{queue="document_ingestion"}) > 300
```
This alert notifies the engineering on-call engineer via PagerDuty. The response team followed standard operating procedures. The on-call engineer quickly identified that the deployment was the likely trigger and gathered the relevant engineers on an incident response bridge by 14:20 UTC. 

### Diagnostic Command Output
During the initial investigation, the DBA ran the following SQL command to inspect active connections and locking states:
```sql
SELECT pid, query, state, age(clock_timestamp(), query_start), wait_event_type 
FROM pg_stat_activity 
WHERE state != 'idle' AND query LIKE '%document_metadata%'
ORDER BY age DESC LIMIT 5;
```
The output of this query confirmed that the migration script was holding a transaction active for over 18 minutes, while 140+ worker connections were in a `Lock` wait state, verifying that the `AccessExclusiveLock` was blocking the connection pool.

---

## [NL-013-S06] Customer Communication

During the incident, our Customer Success team, led by Jordan Lee, initiated our active communication protocol. 

### Status Page Updates
* **14:30 UTC Update:** *"We are currently investigating reports of delayed document uploads and citation indexing in the KnowledgeHub. Core chat and retrieval services remain unaffected."*
* **15:00 UTC Update:** *"We have isolated the root cause affecting document uploads and are actively rolling back a recent patch. We expect services to return to normal within 30 minutes."*
* **15:30 UTC Update:** *"The rollback is complete, and our document processing backlog is clearing. All systems are operating normally."*

### Customer Support Auto-Responder template
The Support Operations team, managed by Elena Costa, configured our Zendesk ticketing system with the following response for customers filing tickets during the outage:
> "Thank you for contacting NovaLearn Support. We are currently experiencing an issue affecting our document upload and indexing pipeline in the KnowledgeHub dashboard. You may see a persistent 'processing' spinner. Our engineering team is actively resolving the issue. Please note that TutorChat remains fully functional for searching previously indexed documents. We will update you once your pending uploads are successfully processed."

### Individual CS Outreach
At 15:45 UTC, a follow-up notification was sent to the admins of the 17 affected customer accounts confirming that the issue was resolved and that all pending documents had been successfully indexed. Jordan Lee personally called the contacts at BrightPath Healthcare and Northstar Financial to apologize for the delay.

---

## [NL-013-S07] Corrective Actions

To prevent a recurrence of this outage and improve our system's resilience, the engineering team has committed to the following corrective actions:

1. **Immediate Retry Cap:** We have updated the background worker configuration to enforce a maximum of 5 retries for any document processing job, using exponential backoff with randomized jitter. (Completed March 15, 2026). The new configuration uses:
   $$\text{Backoff Interval} = 2^{\text{attempt}} + \text{rand}(0, 10) \text{ seconds}$$
2. **Schema Migration Policy:** We have updated our engineering guidelines to mandate that all database schema changes affecting tables larger than 100,000 rows must be executed as multi-stage, lock-free migrations. Foreign key constraints must be created with `NOT VALID` and verified in a separate, low-traffic background process. (Completed March 16, 2026).
3. **Queue Monitoring Alerts:** We will implement granular monitoring for our Redis queue to track retry frequencies and worker connection states independently. (Target completion: June 2026).
4. **Ingestion Load Testing:** Conduct comprehensive load testing of the import pipeline under connection-starvation scenarios to verify that the retry cap holds. (Target completion: July 2026).
5. **Database Pool Isolation:** We will separate database connection pools for write-heavy background tasks and read-heavy user query interfaces. The backend API serving TutorChat will use a dedicated pgBouncer pool, preventing background ingestion failures from starving front-end users. (Target completion: August 2026).

---

## [NL-013-S08] Follow-Up Owners

The following individuals have been assigned ownership of the post-incident action items:

| Action Item | Owner | Target Date | Status | Ticket ID |
|---|---|---|---|---|
| Enforce retry caps and jitter configuration | Leon Grayson (CTO) | 2026-03-15 | Completed | ENG-4102 |
| Update schema migration guidelines | Sofia Almeida (VP Product) | 2026-03-16 | Completed | ENG-4103 |
| Implement granular Redis queue alerts | Leon Grayson (CTO) | 2026-06-30 | In Progress | ENG-4209 |
| Conduct pipeline load testing | David Okafor (Data Analyst) | 2026-07-15 | Planned | OPS-8821 |
| Separate PG pools for writes and reads | Leon Grayson (CTO) | 2026-08-30 | Planned | ENG-4311 |

---

## [NL-013-S09] Lessons Learned

This incident highlighted a critical vulnerability in how our background systems handle failures. While our microservices are logically isolated, shared resources like database connection pools can still propagate failures across the platform. 

We must shift our engineering mindset from "retry until success" to "fail gracefully with backoff." Additionally, this incident reinforced the value of rapid rollback capabilities. Deciding to roll back the deploy at 14:50 UTC was the primary driver of recovery, and we must ensure that our deployment pipeline continues to support rapid, one-click rollbacks for all services. 

Furthermore, we must avoid publishing internal engineering guesses regarding incident impact before database audits are complete. The engineering standup note (NL-022) stating "maybe around 20 customers were affected" caused confusion for Customer Success. We must establish the postmortem database audit as the sole authority for customer-impact metrics.

### Disaster Recovery and Runbook Revisions

In response to these lessons, our operations team has completely revised the database recovery section of the site reliability engineering (SRE) runbook. The updated guide details the process for handling Redis queue backups, manual connection purging, and temporary read-only database failovers during connection pool exhaustion. To ensure SRE teams can execute these procedures under pressure, we will conduct quarterly simulated disaster recovery drills. These simulations will test our team's response to worker timeouts and database lock situations, with a target recovery time objective (RTO) of less than 30 minutes. The results of these drills will be documented in our internal engineering wiki and reviewed by the technical leadership council. By training our SRE staff on isolated connection flushing and load-shedding configurations, we aim to prevent any minor database migration error from escalating into a platform-wide ingestion outage in the future. Each drill must also validate the automated alerting system, ensuring that page duty alerts are sent to the on-call engineer within 120 seconds of any database latency spikes, establishing an early-warning buffer before users encounter errors.
