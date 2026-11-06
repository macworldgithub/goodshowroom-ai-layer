# Initial Administrator: Items for Review

Status: Branch and holding-yard inclusion approved. Site IDs, mappings and the
remaining grant details are still under review. This list creates no grant.

User decision: Keep the canonical site IDs open for now. They must be confirmed
before creating a real business-data scope.

The user reconfirmed this decision after approving branch coverage. Real
administrator setup remains deferred. Do not define site IDs or create real
grants from the branch-name approval alone.

The user selected their Google account as the first administrator and requested
access to all business data. The application requires explicit, approved site,
field, tool, and metric lists. An administrator title is not a wildcard grant.

## Approved branch coverage - 2026-11-03

The user explicitly approved administrator access to all listed branches and
both holding yards. This covers each named branch label in the discovery table,
including DENZA Melbourne, Holding Yard QLD and Holding Yard VIC.
The selected administrator remains `linkmustafa75@gmail.com`.
This resolves the branch-inclusion decision. It does not supply missing stable
IDs, establish alias equivalence, approve unimplemented data tools or create
real database grants. Keep `BYD Dealership` blocked until its actual branch is
identified. Its generic label is not an identified branch or a wildcard grant.

## Items to approve

### Read-only database discovery - 2026-11-03

The user reconfirmed the approved first administrator identity and authorized
database reads to find dealership information. A dealership means a business
branch. The site configuration links each approved branch to its source labels.
The user's intent remains all business data; this discovery creates no grant.

The reads used the configured `LEAD_MONGO_URI` and `DELIVERY_CENTER_MONGO_URI`,
with process settings before `.env.local` before `.env`. TLS certificate checks
remained enabled. No connection values were printed. Direct projected reads
returned only site IDs, names and time zones. Fixed aggregations returned unique
site labels only, without customer fields, individual records or business totals.
No source or application database writes were made.

The initial collection-list command failed with a safe database error (code
8000). Direct field-limited reads succeeded. This does not establish that every
metadata operation is permitted. No permissions or credentials were changed.

Observed labels (a label is not automatically an approved canonical ID):

| Branch or label | Leads site fields | Delivery site field | Review needed |
|---|---|---|---|
| Berwick | BYD Berwick | Not observed | Inclusion approved; stable ID open |
| Campbelltown | BYD Campbelltown | Not observed | Inclusion approved; stable ID open |
| Caroline Springs | BYD Caroline Springs | BYD Caroline Springs | Confirm stable ID |
| Castle Hill | BYD Castle Hill | Not observed | Inclusion approved; stable ID open |
| Doncaster | BYD Doncaster | Not observed | Inclusion approved; stable ID open |
| Fairfield | BYD Fairfield | Fairfield | Confirm these refer to the same branch |
| Frankston | BYD Frankston | Not observed | Inclusion approved; stable ID open |
| Haberfield | BYD Haberfield | Not observed | Inclusion approved; stable ID open |
| Homebush | BYD Homebush | Not observed | Inclusion approved; stable ID open |
| Melbourne / Melbourne City | BYD Melbourne; BYD Melbourne City | Melbourne City | Legacy delivery code normalizes these names together; confirm business equivalence |
| Noosa | BYD Noosa | Not observed | Inclusion approved; stable ID open |
| Nowra | BYD Nowra | Not observed | Inclusion approved; stable ID open |
| Nunawading | BYD Nunawading | BYD Nunawading | Confirm stable ID |
| Wodonga | BYD Wodonga | Not observed | Inclusion approved; stable ID open |
| Wollongong | BYD Wollongong | Not observed | Inclusion approved; stable ID open |
| DENZA Melbourne | DENZA Melbourne | Denza Melbourne | Inclusion approved; confirm cross-source mapping |
| Queensland holding yard | Holding Yard QLD | Not observed | Inclusion approved; stable ID open |
| Victoria holding yard | Holding Yard VIC | Not observed | Inclusion approved; stable ID open |
| Unidentified label | BYD Dealership | Not observed | Identify the actual branch; do not grant access by guessing |

The source `byd-leads-new.dealerships` returned one registry row:
`DENZA Melbourne`, source `_id` `6ac72fad77c7ddc3311f3a83`, timezone
`Australia/Melbourne`. This is a source-qualified dealership ID; it is not yet
an approved AI-layer canonical ID. The checked `byd-panel` metadata collections
(`dealerships`, `sites`, `locations`, `site_settings`) returned no rows.
Do not assume the one dealership registry row is a complete branch list.

The label query used `byd-leads-new.leads.dealer`/`dealerName` and
`byd-panel.clients.site_location`, capped at 100 unique values per source.
It found 20 lead labels and 5 delivery labels. These are label counts, not
customer or business totals. Their presence does not prove an active dealership,
an authorized site or a valid metric. No new IDs were invented.

Next decision: identify the generic label and approve stable canonical IDs and
the cross-source aliases. Inclusion of the listed branches and yards is approved.
Keep unconfirmed mappings blocked. Real grants still require the remaining
dataset, field, tool and metric decisions below.

1. **Sites:** List every dealership the administrator may access. For each one,
   confirm its name and the canonical site identifier used by the application.
   Confirm any source-system names that refer to the same dealership. The current
   project documents do not contain a complete approved canonical list.
2. **Business datasets:** Confirm access to the leads database (`byd-leads-new`)
   and the sales/delivery database (`byd-panel`). Existing scope includes leads
   and dealership context. Delivery, sales, inventory, and contractor questions
   remain conditional extensions unless separately approved and implemented.
3. **Fields:** For each enabled collection, approve the business fields the
   administrator can view. Examples in the approved schema include lead status,
   source, dealership, assignment and dates; customer or delivery identifiers and
   status are conditional on their feature scope. Do not include source-system
   passwords, tokens, integration credentials, or hidden security fields.
   The current proposed lead adapter names `platform`, `leadCreatedDate`,
   `status`, `stage`, `dealer`, `dealerName`, `leadId`, `virtualyardId`,
   `autogateLeadId`, and `customerId`, plus `record_id` for the source identifier.
   These are candidate selectors for review, not proof that all are approved for
   display or that their source values are reliable. Staff fields need a reviewed
   mapping and are not selected by default.
4. **Records:** Confirm the administrator may see all permitted records at each
   approved site, rather than only assigned or selected records. This uses the
   existing `all_permitted` record mode; it does not make unknown sites or
   unapproved fields available.
5. **Questions and tools:** Confirm which implemented questions and tools may use
   those records. The first planned slice is lead counts (`count_leads` and
   `total_leads`); date/source rules must still be verified before real results.
   Other question families and sensitive breakdowns need their own approved rules.
6. **Administrator actions:** Confirm the initial role can manage access and
   settings, and review saved conversations as described in the approved scope.
   Saved-chat review remains separate from business-data grants. Temporary Chat
   is not saved for later review. Existing administrators control later grants.

## Before creating the role

- Review the exact site IDs, source aliases, field selectors, tools and metrics
  against the current adapters. A name alone does not establish a valid mapping.
- Identify the exact isolated test or deployment target before writing records.
  Preserve the operational databases and existing credentials.
- Create or select the reviewed role in the app database and configure its
  explicit scope. The user has not approved a production deployment here.
- Keep ambiguous mappings and unapproved metrics disabled. Do not infer broad
  access from the legacy backends' administrator roles.

Sources: `docs/02-SCOPE.md`, `docs/06-DATABASE-SCHEMA.md`,
`docs/09-SECURITY.md`, and `src/goodshowroom_ai/storage/identities.py`.
