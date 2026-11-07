# Bellhaven CRM Sync

Keeps the CRM's picture of Bellhaven Senior Living (a fictional operator) in line with
Bellhaven's public website: which facilities exist, what they are called, where they are,
and that they hang under the right parent account.

Nothing writes to the CRM without a human clicking **Accept** in the review app.

## Quick start

```bash
pip install -r requirements.txt          # only scrapy, for the scraper
echo "BH_TOKEN=<your token>" > .env      # never committed (.gitignore)

python run_daily.py                      # scrape, pull CRM, match, queue proposals
python review_app.py                     # review at http://localhost:8765
```

On macOS you can double-click `start_review.command` instead of the last line.
`DRY_RUN=1 python review_app.py` lets you click through without writing to the CRM.

## Pipeline

| Step | File | Output |
|---|---|---|
| Scrape every community (name, address, city, state, zip, care offerings, phone) | `scraper.py` (Scrapy) | `data/locations.json` |
| Pull every CRM account | `crm_api.py` | `data/crm_accounts.json` |
| Group CRM accounts into sites, score, classify | `match.py` | `data/matches.json`, `data/crm_sites.json` |
| Turn a decision into exact CRM writes | `proposals.py` | (used by the app) |
| Review page with evidence + Accept / Decline | `build_viewer.py`, `viewer_template.html`, `review_app.py` | `viewer.html`, `data/decisions.json` |
| Daily run | `run_daily.py`, `.github/workflows/daily-sync.yml`, `crontab.example` | `data/pending_proposals.json` |

The scraper starts from the home page, the about page and the paginated `/communities`
list, and follows every `/communities/<slug>` link it finds, because some communities
are linked from the home page but not from the first list page.

## Matching approach

1. **Normalize addresses** with a USPS style abbreviation table, so `1120 West Main Street`
   and `1120 W. Main St` both become `1120 w main st` (directions, street types, saint,
   mount, ordinals, PO Box spellings).
2. **Collapse CRM accounts into sites.** Accounts with the same normalized street and zip
   describe the same building (a duplicate, or the old owner's record left behind after an
   acquisition). 115 facility accounts become 107 sites. Each site keeps every name, phone
   and parent it has in the CRM.
3. **Score each website location against each site** (max 100):

   | Evidence | Points | Why |
   |---|---|---|
   | Street (normalized, exact) | 40 | identifies the physical building |
   | Zip | 15 | |
   | Phone (any phone in the site) | 15 | independent confirmation |
   | Name similarity | 15 | kept low on purpose: renames are what we are trying to catch |
   | Care type | 10 | website "Memory Support" = CRM "Memory Care", etc. |
   | City | 5 | |

4. **Classify**
   - **Confident**: exactly one site scores 80 or more. The best account inside the site is
     the match; the other accounts at that address are duplicates.
   - **Needs review**: no site reaches 80, or two different sites do. The page also runs a
     wide search (looser fuzzy matching, brand words like "Bellhaven" ignored) and lists
     other sites with any overlap, so a reviewer can spot a rename or a move.
   - **No CRM account**: nothing scores at all; the proposal is to create one.
   - **Not on website**: an account under the Bellhaven parent that matches no location.

## What Accept writes

| Situation | CRM writes |
|---|---|
| Match, already under Bellhaven | Update name, street, city, state, zip, phone, care type to the website values (only fields that differ), status Active, note |
| Match under another parent, **no** revenue + AR | Same updates and `parent_id` = Bellhaven |
| Match under another parent, revenue > 0 **and** AR > 0 (**CHOW SOP**) | Old account left exactly as it is except `chow_current_account` = new id; a new account is created under Bellhaven with the website data |
| Other accounts at the same address | `duplicate_of_account` = surviving account, status Inactive, note |
| No CRM account | Create the account under Bellhaven |
| Under Bellhaven but not on the website, **another operator has an account at the same address** | That operator bought it. Revenue + AR: old account left exactly as it is, `chow_current_account` = the buyer's existing account (Sandusky to Millstone). Otherwise: `duplicate_of_account` = buyer's account, Inactive |
| Under Bellhaven but not on the website, no other account at the address | status Needs Review + note. The parent is **not** changed because the new owner is unknown |

Every write adds a dated `[bellhaven-sync]` line to the account's note so a sales rep can
see why it changed. The app re-reads the accounts from the CRM right before writing, so a
plan is never applied to stale data.

## Re-runs are safe

Every Accept / Decline is stored in `data/decisions.json`, keyed by website location and
CRM account id (not by site id, which can change after a CHOW adds an account at the same
address). `run_daily.py` and the review page never offer a decided item again, and a
location can only have one accepted match.

## Design choices

- **Rules, not an LLM, decide the score.** The result is the same every day, costs nothing,
  and every point can be explained to a reviewer ("street exact +40, phone +15"). The cases
  where rules are weakest (same address, completely different name and phone) are routed to
  a human instead of guessed.
- **Duplicates are retired, not merged.** The API has no merge or delete, so the losing copy
  points at the survivor with `duplicate_of_account` and becomes Inactive.
- **Evidence of a buyer beats a guess.** If another operator already has an account at the same address, that is the new owner. If there is no such account, the facility is flagged, not moved.
- **Unknown owner means flag, not guess.** An account that disappeared from the website is
  marked Needs Review rather than moved to a parent we cannot verify.

## What I would build next

- An LLM second opinion (Gemini free tier) only for the needs review cases, shown as extra
  evidence next to the score.
- Scrape the other parent companies' websites, so "not on Bellhaven's website" can become
  "now listed by Cedar Trail" and the re-parent (or CHOW) can be proposed directly.
- Store decisions in a small database instead of a JSON file, with reviewer names.
- Alerting when the scraper finds zero or far fewer locations than yesterday (site redesign).
