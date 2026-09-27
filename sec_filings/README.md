# SEC filings downloader

Downloads a company's SEC filings from EDGAR into a local folder (your Dropbox),
organized as:

```
SEC Filings/
  Sun Country Airlines Holdings, Inc (SNCY)/
    10-K/        10-K, 10-K/A, 20-F, 40-F
    10-Q/
    8-K/         8-K, 8-K/A, 6-K
    Proxy/       DEF 14A, DEFA14A, DEFM14A, PRE 14A, 14C
    Prospectus/  S-1, S-3, S-4, F-1, F-4, 424B*, FWP
    Tender Offer/ SC TO, SC 14D9, SC 13E3 (take-private deals)
    filings_index.csv
```

Forms 3/4/5 (insider trades), 13D/13G and other filings are skipped.

## Setup (one time, on your Mac)

1. Python 3.9+ (`python3 --version` in Terminal; macOS will offer to install it).
2. Create `config.local.json` next to the script containing
   `{"user_agent": "Your Name you@firm.com"}`. SEC requires it
   and blocks requests without it. (This file is git-ignored so your email stays off GitHub.)

## Use

```bash
cd path/to/Local_AI/sec_filings
python3 sec_filings.py SNCY                       # by ticker
python3 sec_filings.py 1135185                    # by CIK (delisted companies)
python3 sec_filings.py "Atlas Air"                # by name
python3 sec_filings.py --comps western_global     # the whole comp set in comps.json
python3 sec_filings.py SNCY --since 2019-01-01    # date cutoff
python3 sec_filings.py SNCY --all-docs            # include exhibits (8-K press releases etc.)
python3 sec_filings.py SNCY --dry-run             # preview only
```

Re-running only downloads filings you don't already have.
