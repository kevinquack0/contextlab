---
source_id: "NL-023"
title: "Sales Discovery Call Transcript  -  Enterprise Prospect"
document_type: "transcript"
status: "transcript"
publication_date: "2026-06-10"
author_department: "Sales"
author_role: "VP Sales"
author_name: "Marcus Reed"
authority_level: 2
intended_audience: "Sales enablement, Customer Success teams"
related_documents: ["NL-003", "NL-007", "NL-009", "NL-012"]
synthetic: true
---

# Sales Discovery Call Transcript  -  Enterprise Prospect

## [NL-023-S01] Call Metadata
* **Date:** June 10, 2026
* **Time:** 11:00 AM  -  11:45 AM EST
* **Location:** Zoom Enterprise Recording ID: REC-99238-APEX
* **CRM Opportunity Link:** SFDC-OPP-2026-88421
* **Participants:**
    * **Marcus Reed**  -  VP of Sales, NovaLearn AI (VP Sales)
    * **Sarah Jenkins**  -  Senior Account Executive, NovaLearn AI (Account Executive)
    * **Michael Chang**  -  Director of Learning & Development, Apex Global Solutions (Prospect)
    * **Linda Torres**  -  VP of Security & Compliance, Apex Global Solutions (Prospect)
* **Reference Policies Circulated:** NL-007_security_data_governance_policy.md (draft summary), NL-008_ai_assistant_reliability_policy.md (excerpt).

*(Note: This call transcript is classified as an informal sales discovery record with an authority level of 2. It contains conversational exchanges, customer requirements, and non-binding explanations by sales representatives. It does not establish binding contractual terms or official policy. For verified specifications on pricing, security protocols, or product roadmaps, refer to NL-003, NL-007, and NL-014, which carry authority levels of 5 and 4.)*

---

## [NL-023-S02] Prospect Context
Apex Global Solutions is a global logistics and supply chain services provider with approximately 3,500 employees across North America. The organization is undergoing a digital transformation of its employee enablement systems. They have a massive repository of Standard Operating Procedures (SOPs), customer-support guides, compliance manuals, and safety protocols, currently scattered across multiple legacy document repositories. They are seeking an AI-powered assistant to help their customer-facing teams and warehouse staff retrieve accurate training and compliance information.

---

## [NL-023-S03] Transcript  -  Business Need
**Marcus Reed:** 
"Well, Michael, Linda, thank you for jumping on the phone with us today. Sarah and I are really excited to show you what NovaLearn can do. To kick things off, Michael, I'd love to hear a bit more about your current setup at Apex. What are the main pain points your L&D team is facing, and what would a successful AI deployment look like for you?"

**Michael Chang:** 
"Thanks, Marcus. Glad to be here. To put it bluntly, our internal knowledge base is a disaster. We have been growing quickly over the last five years, and we've acquired three smaller logistics companies. As a result, we have training documentation, safety procedures, and customer support templates spread across two different SharePoint servers, a legacy Confluence instance, and random shared network drives. 

Our customer support agents - we have about 800 of them - spend a massive amount of time just trying to find answers when customers call. They are searching through multiple tabs, and they often give conflicting information because they find outdated documents. For example, our shipping policies changed in January, but we still have agent guides from 2024 active on one of our SharePoint drives. 

And on the warehouse side, we have strict OSHA safety guidelines and Department of Transportation compliance rules for our drivers. If a warehouse employee needs to look up the protocol for handling hazardous material or a driver needs to check truck maintenance logs, they need the exact answer immediately. Right now, they have to sift through a 300-page PDF manual on a desktop computer. We want an AI assistant where they can type a question and get an immediate, accurate answer with a link to the exact source page. We need absolute accuracy here because a compliance violation in safety protocols can cost us thousands in fines."

**Sarah Jenkins:** 
"That is a classic scenario we see all the time, Michael. NovaLearn was built precisely to solve this type of scattered documentation problem. Our KnowledgeHub module handles the ingestion from SharePoint, local files, and help centers, and our TutorChat interface acts as the single assistant that employees can query. The key is that TutorChat doesn't just guess the answer; it extracts it from your approved files and provides a direct, clickable citation. 

Let me ask: are you thinking of rolling this out to all 3,500 employees at once, or are you planning a pilot?"

**Michael Chang:** 
"We definitely want to start with a pilot. We were thinking of rolling it out first to our Customer Support team, which is about 150 users. Actually, I was speaking with a colleague at another company who used a competitor, and he recommended that we start with our smallest, most agile team - our digital product team, which is only about 30 people. He suggested that startups and small teams are much easier to roll out, and we'd see higher adoption metrics. Do you find that to be the case? Is it better to focus on small teams first rather than our core operations? My main concern is that if we go too big too fast, we'll run into user friction, and people will just stop using the assistant if they get bad answers in the first week."

**Sarah Jenkins:** 
"Oh, absolutely. We love starting with small, agile teams. Startups and small business segments represent a major sweet spot for us in terms of speed. We can set up a small team in just a couple of weeks, and the adoption is usually off the charts because they are quick to adapt and don't have legacy processes. We can get a pilot live for a small group very quickly. 

That said, our platform is fully enterprise-grade, and we have many large clients. But starting with a smaller group like your L&D administrative staff or a small support pod is a great way to build momentum. However, our data metrics show that the real long-term business impact occurs when we scale to your core operational teams, like the 800 support agents, where the reduction in average handle time translates directly to dollar savings."

**Michael Chang:**
"But doesn't that require a lot more cleanup? I mean, our product team's documentation is relatively clean and markdown-based. Our customer support documents are a mix of legacy PDFs, old DOCX files, and spreadsheets. If we pilot with the support team, aren't we risking failure due to the messiness of their files?"

**Sarah Jenkins:**
"It is a bit more work upfront, yes, but we guide you through that. We assign a dedicated customer success resource to help structure the KnowledgeHub ingestion. We believe the pilot is best run where the pain is highest, which is support, rather than a small team that already has good retrieval workflows."

*(Note: While Sarah Jenkins notes sales enthusiasm for the startup/small team segment, NovaLearn's official business metrics in NL-012 show that SMB and startup clients have a much higher logo churn of 4.8% and lower expansion rates, whereas mid-market and enterprise segments demonstrate the highest Net Revenue Retention (116%) and customer satisfaction (NPS of 52 for Enterprise, 47 for Mid-Market vs. 31 for SMB). The recommendation for mid-market and enterprise clients is to run pilots with 100 to 300 users in high-impact departments to justify the onboarding investment.)*

---

## [NL-023-S04] Transcript  -  Integrations
**Michael Chang:** 
"That makes sense. In terms of where the employees will interact with the assistant, we are a heavy Microsoft Teams organization. Our support agents have Teams open all day, and our warehouse managers use the Teams mobile app. We really want the assistant to live inside Teams. We don't want them having to open another browser tab or log into a separate portal. Do you have a Teams integration?"

**Marcus Reed:** 
"Yes, absolutely. The Microsoft Teams integration is one of our most popular requests, and I have great news: it is basically right around the corner. Our engineering team has a working connector, and it's almost ready for general release. We are putting the finishing touches on it. You'll be able to install the NovaLearn bot directly from the Teams app store, pin it to the sidebar, and your employees can interact with it just like they are chatting with a colleague. It will support the exact same permission structures and citations as our web interface."

**Michael Chang:** 
"Oh, that's perfect. If we can get that up and running for the pilot, that would be a huge win. When you say 'almost ready,' what does that mean? Can we include it in our pilot starting in July?"

**Marcus Reed:** 
"I don't see why not. We are in the final stages. We can talk to our implementation lead, Lina Park, about setting you up with our early access build for the pilot. It's basically done, so we should be able to turn it on for your workspace when we launch."

*(Note: This is a significant sales overclaim. The official Product Roadmap and Release Notes, NL-014, states that the Teams integration is currently 'in design / planned for Q4 2026' and is not generally available. Furthermore, the internal Slack thread NL-027 reveals that the Teams connector is in a very early proof-of-concept phase, lacking security controls and tenant isolation, and is far from ready for customer deployment.)*

**Michael Chang:** 
"That's great. What about other integrations? We use Zendesk for our external support tickets. Can NovaLearn pull information from solved Zendesk tickets to help our agents?"

**Sarah Jenkins:** 
"Yes! We have an approved Zendesk integration connector. It can sync with your solved ticket database and index the resolution steps. This is a standard feature on both our Growth and Enterprise plans. It takes about an hour to configure."

---

## [NL-023-S05] Transcript  -  Security and Retention
**Linda Torres:** 
"I want to jump in on the security and data governance side. I'm the one who has to sign off on the security review. We are handling sensitive shipping logs, customer addresses, and proprietary warehouse routing procedures. How does NovaLearn handle customer data isolation? Is our data co-mingled with other clients in your database?"

**Sarah Jenkins:** 
"That is a critical question, Linda. Security is the foundation of our platform. We use logical isolation for all customer workspaces. Each customer has their own dedicated database container and encryption keys. Your data is completely isolated at rest and in transit. We never co-mingle your documents or search queries with other tenants, and your data is never used to train our base AI models. We are SOC 2 Type II certified and undergo annual third-party penetration testing."

**Linda Torres:** 
"Okay, logical isolation is standard. What about data retention and deletion? If we run a pilot and decide not to move forward, or if we eventually terminate our contract, what is the deletion policy? We require that our data be wiped completely from your systems. How long does that take?"

**Sarah Jenkins:** 
"We fully respect your data ownership. If you terminate the contract or request deletion, we delete your data immediately from all our active databases and indexes. Nothing remains on our live servers. We make sure that once you say go, the data is completely wiped. Our engineering team has a script that truncates all workspace tables and marks the file blocks for overwriting."

**Linda Torres:** 
"That sounds fine verbally, but is that immediate deletion guaranteed in the standard contract? Specifically, does the 'immediate' deletion apply to cold storage backups? Our legal team requires a signed DPA that explicitly guarantees deletion of all customer data, including backups, within 30 days of termination."

**Sarah Jenkins:** 
"Yes, we can write that into the custom terms for your agreement. We pride ourselves on being compliance-friendly, so we can ensure immediate deletion upon contract termination is part of our commitment to Apex. We do this for our larger clients regularly."

*(Note: This statement is an informal sales claim that conflicts with official policy. The Security and Data Governance Policy, NL-007, states that standard data deletion occurs 'within 90 days after contract termination' to allow for backup cycles and system audits. Custom immediate deletion requires special approval and is not standard, despite the sales rep's verbal assurances. In practice, NovaLearn's standard backup rotation schedule is 90 days, making a 30-day backup scrub technically unfeasible without manual engineer intervention, which is highly restricted.)*

**Linda Torres:** 
"Okay, we'll need that in writing in the DPA. Now, what about SSO? We use Okta for all employee access. Do you support SAML-based SSO?"

**Sarah Jenkins:** 
"Yes, Okta SSO is fully supported. It is included in our Enterprise plan. It allows you to enforce multi-factor authentication (MFA) and manage user provisioning directly through your Okta portal."

**Linda Torres:** 
"And what about role-based access? If we ingest our employee directories, can we map active directory roles to document sets? For example, we don't want warehouse staff seeing HR policies about payroll, and vice versa."

**Marcus Reed:** 
"Yes, absolutely. Our Enterprise plan supports advanced role-based access controls (RBAC) and permission mapping. You can sync your active directory groups directly to our system, and define rules like 'Only users in the HR-Group AD role can query documents in the HR-Manuals workspace.' When a user queries TutorChat, the system checks their active session permissions in real-time and filters out any answers derived from documents they don't have access to. They won't even know those documents exist."

---

## [NL-023-S06] Transcript  -  Pricing Discussion
**Michael Chang:** 
"Let's talk about pricing. How is the platform packaged, and what kind of cost are we looking at for Apex?"

**Marcus Reed:** 
"We package our platform based on features, admin seats, and the number of learners. We have three main plans: Starter, Growth, and Enterprise. 

The Starter plan is $1,200/month, which includes 500 learners and 3 admin seats, with standard email support. 
The Growth plan is $3,600/month, which includes 2,000 learners and 10 admin seats, with priority support. 
The Enterprise plan starts at $8,500/month and is fully customizable. It includes custom learner limits, unlimited admin seats, dedicated customer success management, advanced security integrations like Okta SSO, custom role-based permission mapping, and extended audit log retention.

Given that Apex has 3,500 employees, requires SSO integration, complex active directory permission mapping, and exportable compliance audit logs, you would fit perfectly into our Enterprise tier. We would structure a custom enterprise contract for you based on the initial pilot size of 150 users, with a pre-negotiated expansion path as you roll it out to the remaining support and warehouse staff. 

Our enterprise billing can be structured annually or multi-year, and we offer volume discounts as you add more learners."

**Michael Chang:** 
"And what about implementation and onboarding? Is there a fee for that?"

**Sarah Jenkins:** 
"Yes, we charge a one-time onboarding fee to cover the cost of our implementation team, who will work with your IT staff to set up the integrations, map your permissions, and audit your document corpus. For our Growth plan, the standard onboarding fee is $7,500. For our Enterprise plan, the onboarding fee starts at $15,000, depending on the complexity of your security setup and the volume of documents we are ingesting. 

Lina Park, our implementation lead, would be assigned to your account. She will guide you through our standard 8-to-10 week enterprise onboarding process, which includes a weekly status call, SSO configuration, and a citation accuracy audit before the pilot goes live."

**Michael Chang:** 
"Eight to ten weeks? That seems a bit long. I thought you said we could get a pilot up in a couple of weeks? We have our peak logistics season starting in September, and we need our support agents trained on the new shipping procedures by then. If onboarding takes 10 weeks, we won't be live until mid-August, which leaves us almost no time for user testing and feedback loops. Is there any way to fast-track this? E.g., if we defer the Okta SSO integration to a later phase, or if we bypass some of the legacy document cleanup, can we get the pilot live in 4 weeks?"

**Sarah Jenkins:** 
"For a small, basic setup on the Starter or Growth plan with no custom integrations, we can move very fast - sometimes 4 weeks. But for an Enterprise account with SSO, active directory mapping, and legacy documents, the 8-to-10 week timeline is our standard checklist to guarantee the system meets our 85% citation correctness threshold and passes your internal security review. We want to make sure it's done right. If we bypass the data cleanup phase, we risk indexing outdated or scanned documents without OCR, which will cause TutorChat to hallucinate or fail to cite sources, ultimately violating our AI reliability standards."

**Marcus Reed:** 
"Exactly. We don't want to rush the security setup. Linda, I'm sure you appreciate that we take the time to map the permission roles correctly so that a warehouse employee can't accidentally query a restricted HR document."

**Linda Torres:** 
"Yes, I definitely prefer a thorough setup over a rushed one. What about document volume? Is there a limit on how many files we can upload on the Enterprise plan?"

**Marcus Reed:** 
"On the Enterprise plan, we offer custom document storage limits. Typically, we start you with 100 gigabytes of storage, which is enough for hundreds of thousands of PDF pages. If you exceed that, we have very reasonable storage expansion fees. We also have a monthly page indexing limit for scanned documents that require OCR, which we can customize based on your repository size."

---

## [NL-023-S07] Sales Rep Follow-Up Notes
* **Prospect:** Apex Global Solutions
* **Key Contacts:** Michael Chang (L&D), Linda Torres (Security)
* **Estimated Deal Size:** $8,500/month ($102,000 ARR) + $15,000 implementation fee (Enterprise Plan).
* **Key Requirements:** Microsoft Teams integration (critical driver for the deal), Active Directory permission mapping (due to HR and warehouse document segregation), Okta SSO, compliance audit logs, and custom data deletion guarantees (requested 30-day backup scrub clause).
* **Next Steps:**
    1. Send Linda the Security and Data Governance Policy (`NL-007`) and the AI Assistant Reliability Policy (`NL-008`). Note: need to prepare her for the 90-day standard deletion policy if they push back on standard terms.
    2. Follow up with Leon Grayson to confirm we can get them onto the 'early access' Teams integration build for their July pilot. (Sales rep note: Leon will likely push back given the standup notes and Slack discussions about tenant isolation, but we must advocate for this to secure the ARR).
    3. Draft a custom Enterprise proposal with a 150-user pilot scope, pre-negotiating standard expansion seats up to 1,000 learners as customer support scales.
    4. Coordinate with Lina Park to draft a preliminary 'Corpus Readiness Checklist' specifically highlighting the scanned PDF and OCR requirements to manage Michael's expectations about the 8-to-10 week timeline.
* **Sales Rep Assessment:** High-intent buyer. L&D is feeling major pain with search time, and Security is engaged. The primary hook is the Teams integration. We must position this as almost ready to keep the deal moving, even if engineering says Q4. We can handle the delay during onboarding. If we don't present Teams as a near-term delivery, they might look at LearnPilot.
