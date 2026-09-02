"""DigitalPaani's client-email house style.

Source: the CS team's "Digital Paani CS Email Assistant" context (built from
real Mohit Joshi emails) — the company writes to clients as one voice, not as
individuals. Jarvis drafts with Claude rather than the original Groq/llama
setup, so what carries over is the *style*: voice rules, fixed tones, and the
template skeletons. What deliberately does not: the sign-off identity. Jarvis
serves every employee, so signatures are built from the signed-in user.

The compact voice rules ride in the system prompt (they apply to any client
email); the full skeletons are served on demand by the email_template tool so
25 templates don't tax every unrelated turn.
"""

from __future__ import annotations

VOICE_RULES = """\
# DigitalPaani client-email voice (house style — the company speaks as one)
When drafting ANY email to a client, follow the house style:
- Before drafting, call email_template with the closest template name; it
  returns the skeleton, the required tone, and the variables to collect. Ask
  the user for at most TWO missing details, then draft.
- Greetings: one man = "Dear Sir," | unknown/mixed = "Dear Sir/Ma'am," |
  group = "Dear Team," | informal reply to a known person = "Hi Sir,"/"Hi Ma'am,"
- Open with "I hope you are doing well." for routine or positive mail (MBR,
  QBR, case study, feature, payment, NPS, welcome, onboarding, follow-up,
  renewal). SKIP it for delays, technical updates, proposals, issue
  acknowledgements, and site visit reports.
- Never write: "touch base", "circle back", "synergy", "as per my last
  email", "hope this email finds you well".
- House phrases: "Please find attached…", "Kindly let us know if you have any
  queries", "We will do the needful", "We will continue to closely monitor",
  "This is a gentle reminder" (payments), "This is to inform you that"
  (technical), "You can verify the same on the Digital Paani dashboard".
- Short paragraphs (max 3 lines). Bullets for features/results/observations;
  numbered lists for MBR updates and recovery plans. End with a next step.
- Payment emails MUST contain the plain-text invoice table:
  Date | Bill No. | Site Name | Amount — plus a Total row.
- Sign off with the sender block email_template provides (the signed-in
  user's name, never someone else's).
These rules are for CLIENT-facing email. Internal mail to colleagues stays
normal and brief.
"""

# The real escalation matrix (reference data — welcome emails ship a BLANK
# matrix for the CSM to fill, because it varies by service model).
ESCALATION_MATRIX = """\
Level 1: Indra Prakash Pandey (Service Manager)          | 8299326026 | indraprakash.pandey@digitalpaani.com
Level 1: Mohit Joshi (Customer Success Manager)          | 7310777062 | mohit.joshi@digitalpaani.com
Level 2: Rahul Singh (Lead - Customer Service)           | 8077061354 | rahul.singh@digitalpaani.com
Level 3: Hemant Maheshwari (Head - Customer Success & Delivery) | 9752881701 | hemant.maheshwari@digitalpaani.com"""

# template key -> (title, fixed tone or "", variables to collect, skeleton).
# Skeletons keep the source material's structure; [square brackets] mark what
# the drafter fills. Where the source shipped only a format line, that line
# is the skeleton.
TEMPLATES: dict[str, tuple[str, str, list[str], str]] = {
    "welcome_om": (
        "Welcome Email — O&M Model", "Friendly",
        ["Client/company name", "Site name", "O&M scope",
         "Escalation hierarchy (leave blank lines if not final)"],
        """Subject: Welcome to DigitalPaani — [Company Name]

Dear Sir,

I hope you are doing well.

We are delighted to welcome [Company Name] as our valued client for Operations and
Maintenance services. Thank you for choosing DigitalPaani as your water management
partner. We are committed to ensuring smooth, reliable, and efficient day-to-day
operations and maintenance of your [STP/WTP] plant.

I am [Sender Name], your dedicated Customer Success Manager. I will work closely with
your team to ensure seamless plant operations, coordinate with our on-ground O&M team,
provide regular maintenance updates, and resolve any operational concerns at the earliest.

For any queries, please reach out through our escalation matrix:
Level 1: _____________ (O&M Supervisor)    | Ph: _____________ | _____@digitalpaani.com
Level 1: _____________ (CSM)               | Ph: _____________ | _____@digitalpaani.com
Level 2: _____________ (Service Manager)   | Ph: _____________ | _____@digitalpaani.com
Level 3: _____________ (Head – Operations) | Ph: _____________ | _____@digitalpaani.com

We are excited to begin this journey and look forward to achieving operational
excellence together.

[WELCOME SIGN-OFF]""",
    ),
    "welcome_tech": (
        "Welcome Email — Tech Model", "Friendly",
        ["Client/company name", "Site name", "DP-enabled services",
         "Escalation hierarchy (leave blank lines if not final)"],
        """Subject: Welcome to DigitalPaani — [Company Name]

Dear Sir,

I hope you are doing well.

We are delighted to welcome [Company Name] as our valued client. Thank you for
choosing DigitalPaani as your water intelligence and technology partner. We are
committed to transforming your water management through our advanced IoT monitoring
platform, real-time dashboards, and data-driven insights.

I am [Sender Name], your dedicated Customer Success Manager. I will ensure successful
deployment of our digital platform, guide your team on maximising platform value,
and keep you informed of system performance at all times.

Your DigitalPaani-enabled services:
- Real-time IoT-based water quality and quantity monitoring
- Automated alerts and threshold-based WhatsApp/email notifications
- Live dashboard with historical data and trend analytics
- Regular data-driven performance and compliance reports

For any queries, please reach out through our escalation matrix:
Level 1: _____________ (Service Manager)       | Ph: _____________ | _____@digitalpaani.com
Level 1: _____________ (CSM)                   | Ph: _____________ | _____@digitalpaani.com
Level 2: _____________ (Lead – Customer Svc)   | Ph: _____________ | _____@digitalpaani.com
Level 3: _____________ (Head – CS & Delivery)  | Ph: _____________ | _____@digitalpaani.com

We look forward to a successful and long-lasting partnership.

[WELCOME SIGN-OFF]""",
    ),
    "onboarding_plan": (
        "Onboarding Plan", "Professional",
        ["Client name", "Project phases with timelines", "Expected go-live date",
         "Support needed from client"],
        """Subject: Onboarding Plan — [Company Name] | [Project Name]

Dear Sir,

I hope you are doing well.

Please find below the detailed onboarding plan for [Company Name / Site Name]
outlining key milestones, timelines, and responsibilities.

Phase 1: [Name] | Duration: [X days] | Start: [Date] | Owner: DigitalPaani Team
  - [Activity 1]
  - [Activity 2]

Phase 2: [Name] | Duration: [X days] | Start: [Date] | Owner: [Owner]
  - [Activity 1]
  - [Activity 2]

Expected Go-Live: [Date]

Support required from [Company Name]:
  - [Requirement 1 — e.g. Site access from Day 1]
  - [Requirement 2 — e.g. Electrical connection at Panel Room by Day 3]

Kindly review and let us know if you have any questions.

[SIGN-OFF]""",
    ),
    "onboarding_update": (
        "Onboarding Update", "Formal",
        ["Completed tasks", "On-track items", "Delayed tasks with reasons",
         "Recovery plan", "Help needed from client"],
        """Subject: Onboarding Update — [Company Name] | Week [X] | [Date]

Dear Sir,

I hope you are doing well.

Please find below the onboarding status update for [Site Name] as of [Date].

Completed:
  - [Task 1] — [completion date]

On Track:
  - [Task 3] — expected by [date]

Delayed:
  - [Task] | Original: [date] → New: [date]
    Reason: [clear reason]
    Recovery: [steps to resolve]

Support needed from your end:
  - [Ask 1 — e.g. Panel room access required by 20 May]

Overall: [X]% complete. Go-live remains [on track for / revised to] [Date].

[SIGN-OFF]""",
    ),
    "progress_update": (
        "Standard Progress Update", "Professional",
        ["Project type (Retrofit/O&M/Installation)", "Completed work",
         "In-progress work", "Upcoming milestones"],
        """Subject: Progress Update — [Work Type] | [Company Name]

Sections: Completed / In Progress / Upcoming / Status / Next milestone.
Short paragraphs; bullets per section; end with the next milestone date.

[SIGN-OFF]""",
    ),
    "off_track": (
        "Off Track Update", "Apologetic",
        ["Delayed task", "Original deadline", "New deadline",
         "Honest reason for delay", "Step-by-step recovery plan"],
        """Subject: Update on Delay — [Task/Project] | [Company Name]

Dear Sir,

I want to sincerely apologise for the delay in [specific task] at [Site Name].

What is delayed: [Task] — was due [date], now expected [new date]
Reason: [Honest explanation]
Impact: [What this affects]

Our plan to recover:
1. [Step 1] — by [date] — [owner]
2. [Step 2] — by [date] — [owner]

We take full responsibility and I will personally monitor progress with daily
updates until resolved. We sincerely appreciate your patience.

[SIGN-OFF]

(NOTE: no "I hope you are doing well" — apologise first.)""",
    ),
    "onboarding_closure": (
        "Onboarding Closure", "Friendly",
        ["Client name", "All completed items summary", "Go-live date",
         "Dashboard access link"],
        """Subject: Onboarding Complete — Welcome Aboard [Company Name]!

Summary of everything set up, the dashboard link, and a warm thank-you for
the client's cooperation during onboarding.

[SIGN-OFF]""",
    ),
    "case_study": (
        "Case Study Sharing", "",
        ["Client name", "Compliance result", "Energy saving % and Rs",
         "Chemical saving % and Rs", "Maintenance saving", "Total cost saving/year"],
        """Subject: Case Study: [X]-Year Impact of Digital Paani at [Client Name]

Core Impact Summary (keep this exact table shape):
+------------------------------------------+
| Regulatory Compliance  : 100% Achieved   |
| Energy Saving          : [X]% | Rs [X]L  |
| Chemical Saving        : [X]% | Rs [X]L  |
| Maintenance Saving     : [X]% | Rs [X]L  |
| Total Cost Saving      : Rs [X]L/year    |
+------------------------------------------+

Key findings as bullets; close by inviting a review meeting.

[SIGN-OFF]""",
    ),
    "mbr": (
        "Monthly Insights / MBR", "",
        ["Client name", "Month", "Water consumed KL", "Efficiency score",
         "Alerts triggered/resolved", "Key highlight or issue"],
        """Subject: Monthly Insights — [Company Name] | [Month]

Numbered sections: 1. Highlights  2. Key Metrics (KL, efficiency, alerts)
3. Notable issue or achievement. Close with "We will continue to closely
monitor…".

[SIGN-OFF]""",
    ),
    "qbr": (
        "QBR Sharing", "",
        ["Client name", "Quarter", "Water savings KL and Rs", "Uptime %",
         "Key highlight", "Impact numbers", "Next steps"],
        """Subject: QBR — [Company Name] | Q[X]

Highlights & Impact section (savings in KL and Rs, uptime, cost impact),
then Next Steps as a numbered list.

[SIGN-OFF]""",
    ),
    "nps_followup": (
        "NPS Follow-up", "",
        ["Client name", "NPS score (0-10)", "Specific feedback received"],
        """Subject: Thank You for Your Feedback — [Company Name]

Thank them for the score; if low, acknowledge specifics and say what will
change; if high, appreciate and invite continued feedback.

[SIGN-OFF]""",
    ),
    "demo_followup": (
        "Follow-up After Demo", "",
        ["Client name", "Demo date", "Key points discussed",
         "Agreed next steps", "Follow-up date"],
        """Subject: Follow-up — DigitalPaani Demo | [Company Name]

Recap the demo's key points as bullets, restate agreed next steps, and
propose the follow-up date.

[SIGN-OFF]""",
    ),
    "payment_followup": (
        "Payment Follow-up", "",
        ["Client name", "Each invoice: date, bill no., site name, amount",
         "Total outstanding amount"],
        """Subject: Payment Follow-up — Outstanding Invoices for [Company Name]

Dear Sir/Ma'am,

I hope you are doing well.

This is a gentle reminder regarding the pending payments for [Company Name]:

Date        | Bill No.       | Site Name     | Amount
------------|----------------|---------------|-------------
[DD-Mon-YY] | [SER/XXXX/XXX] | [Site Name]   | Rs [X,XXX]
            |                | Total Amount  | Rs [X,XXX]

Please process the payment at your earliest convenience. Kindly let us know
if you require any supporting documents from our end.

Looking forward to your prompt response.

[SIGN-OFF]""",
    ),
    "renewal": (
        "Renewal Reminder", "Urgent",
        ["Client name", "Contract end date", "Current plan",
         "3 key achievements this year"],
        """Subject: Renewal Due — [Company Name] | [Contract]

Due in [X] days. Current plan, three key achievements this year as bullets,
and a request to confirm renewal by [date].

[SIGN-OFF]""",
    ),
    "proposal": (
        "Upsell / Proposal", "",
        ["Client name", "Proposal type", "Scope of work",
         "Changes from previous version"],
        """Subject: Proposal — [Type] | [Company Name]

"As requested, please find attached…" — outline the scope of work, call out
any changes from the previous version, and invite discussion. (No "I hope
you are doing well" for proposals.)

[SIGN-OFF]""",
    ),
    "churn_risk": (
        "Churn Risk Outreach", "",
        ["Client name", "How long inactive", "Known reason for low activity"],
        """Subject: Checking In — [Company Name]

Warm, no-pressure check-in: note the inactivity without accusing, offer a
session to get more value from the platform, invite a call.

[SIGN-OFF]""",
    ),
    "client_decision": (
        "Decision at Client End", "Urgent",
        ["Client name", "What decision is pending", "Impact of delay",
         "Required by which date"],
        """Subject: Action Required — [Item] | [Company Name]

What is pending, the current impact of the delay, and a specific response
date.

[SIGN-OFF]""",
    ),
    "feature_announcement": (
        "Feature Announcement", "",
        ["Feature name", "3-4 key benefits", "Which clients get it",
         "Configurable parameters"],
        """Subject: New on DigitalPaani — [Feature Name]

Benefits as bullets, who is getting it, and what they can configure.

[SIGN-OFF]""",
    ),
    "issue_ack_reactive": (
        "Issue Acknowledgement — Unknown (Reactive)", "Apologetic",
        ["Site name", "Brief issue description", "Immediate actions taken",
         "Next update time"],
        """Subject: Acknowledgement — Issue at [Site Name] | [Company Name]

Dear Sir,

We sincerely apologise for the disruption caused by [brief issue description]
at [Site Name]. We have received your report and want to assure you this is
being treated as our highest priority.

What we know so far:
[Brief description of observed symptoms]

Root cause: Under active investigation. Technical team mobilised immediately.

Immediate actions taken:
- [Action 1]
- [Action 2]

Resolution plan: Detailed plan to follow once root cause is confirmed.
Next update by [Date/Time].

We deeply regret the disruption and take full ownership of this issue.

[SIGN-OFF]

(NOTE: no "I hope you are doing well" — apologise first.)""",
    ),
    "issue_ack_proactive": (
        "Issue Acknowledgement — Proactive (Known)", "Apologetic",
        ["Site name", "What/when/affected/impact", "Root cause",
         "Resolution plan with dates", "Prevention measures"],
        """Subject: Proactive Issue Update — [Site Name] | [Company Name]

Incident details (what / when / affected / current impact), root cause,
numbered resolution plan with dates, expected full resolution, prevention
measures. Close: "Status is live on your Digital Paani dashboard." and a
sincere apology.

[SIGN-OFF]""",
    ),
    "issue_major": (
        "Issue Update — Major", "Urgent",
        ["Issue description", "Systems affected", "Operational impact",
         "Immediate action required from client", "Decision needed by when"],
        """Subject: URGENT: Critical Issue — [Description] | [Company Name]

Severity: MAJOR. Issue / Reported / Affected / Operational impact / Status.
Immediate action REQUIRED from the client as a numbered, time-bound list.
"Decision required by: [SPECIFIC TIME TODAY]". Our team's actions with ETAs.
Next update within hours. Sincere apology; strong, direct language.

[SIGN-OFF]""",
    ),
    "issue_minor": (
        "Issue Update — Minor", "",
        ["Issue description", "Current status", "Actions taken so far",
         "Expected resolution date"],
        """Subject: Issue Update — [Description] | [Company Name]

Issue and severity (Minor), status, actions taken as bullets,
"Impact: Minimal — no disruption to operations.", expected resolution date.

[SIGN-OFF]""",
    ),
    "site_visit": (
        "Service Visit Report", "",
        ["Site name", "Visit date", "Technician name", "Key observations",
         "Recommended actions"],
        """Subject: Service Visit Report — [Site Name] | [Visit Date]

"This is to inform you that…" — visit details, key observations as bullets,
recommended actions. (No "I hope you are doing well" for visit reports.)

[SIGN-OFF]""",
    ),
    "escalation_response": (
        "Escalation Response", "Apologetic",
        ["What went wrong", "Client impact", "Steps being taken",
         "Timeline to resolve"],
        """Subject: Response to Your Escalation — [Company Name]

Apologise first. What went wrong, the impact on the client, the steps being
taken with owners, and a concrete timeline. Offer the escalation matrix.

[SIGN-OFF]""",
    ),
    "refund_dispute": (
        "Refund / Dispute", "",
        ["Client name", "Amount in dispute", "Reason", "Resolution offer"],
        """Subject: Regarding Your [Refund/Billing] Query — [Company Name]

Acknowledge the dispute plainly, state the amount and the facts, present the
resolution offer, and invite a call to close it out.

[SIGN-OFF]""",
    ),
}


def signoff(name: str, phone: str = "", role: str = "",
            welcome: bool = False) -> str:
    """The sender block, built from the signed-in user.

    The source material hardcodes one CSM's identity; Jarvis signs as
    whoever is speaking. Lines without data are omitted rather than left as
    placeholders a draft could leak.
    """
    lines = ["Best regards," if welcome else "Thanks & Regards", name]
    if role:
        lines.append(role)
    lines.append("DigitalPaani")
    if phone:
        lines.append(phone)
    return "\n".join(lines)
