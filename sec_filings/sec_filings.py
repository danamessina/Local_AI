#!/usr/bin/env python3
"""Download SEC EDGAR filings for a company (or a set of comps) into a local folder.

Pulls annual/quarterly reports, current reports, proxies and registration
statements / prospectuses. Skips insider (Forms 3/4/5) and ownership filings.

Examples:
    python3 sec_filings.py SNCY                  # by ticker
    python3 sec_filings.py 1135185               # by CIK (works for delisted companies)
    python3 sec_filings.py "Atlas Air"           # by name search
    python3 sec_filings.py --comps western_global
    python3 sec_filings.py SNCY --since 2020-01-01 --all-docs
    python3 sec_filings.py 1135185 --since 2019-01-01 --until 2020-10-20
    python3 sec_filings.py SNCY --dry-run        # list what would be downloaded
    python3 sec_filings.py SNCY --email you@firm.com   # instead of config.local.json
    python3 sec_filings.py SNCY --text           # also write clean .txt copies for Claude / AnythingLLM

Uses only the Python standard library. Re-running is safe: files already on
disk are skipped, so a re-run only fetches new filings.
"""

import argparse
import csv
import gzip
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Used when config.json is absent, so the script also works as a single downloaded file.
DEFAULT_CONFIG = {
    "user_agent": "YOUR NAME your.email@example.com",
    "contact_name": "Dana Messina",  # SEC asks for a name and email in the User-Agent
    "output_dir": "/Users/danamessina/KM Server Dropbox/KM Server Team Folder/Consulting/DOL/2025/"
                  "Western Global/SEC Filings",
    "since": None,
    "until": None,
    # intake-filing output (the raw/ download cache stays next to the script)
    "intake_output_dir": "/Users/danamessina/KM Server Dropbox/KM Server Team Folder/Consulting/DOL/2025/"
                         "Western Global/SEC Filings",
}

# Form type -> folder name. Amendments ("/A") map to the same folder as the base form.
FORM_GROUPS = {
    "10-K": "10-K", "10-KT": "10-K", "10-K405": "10-K",
    "20-F": "10-K", "40-F": "10-K",  # foreign-issuer annual reports
    "10-Q": "10-Q", "10-QT": "10-Q",
    "8-K": "8-K", "6-K": "8-K",  # 6-K is the foreign-issuer equivalent of 8-K
    "DEF 14A": "Proxy", "DEFA14A": "Proxy", "DEFM14A": "Proxy", "DEFR14A": "Proxy",
    "PRE 14A": "Proxy", "PREM14A": "Proxy", "PRER14A": "Proxy",
    "DEF 14C": "Proxy", "PRE 14C": "Proxy", "DEFM14C": "Proxy", "PREM14C": "Proxy",
    "S-1": "Prospectus", "S-1MEF": "Prospectus", "S-3": "Prospectus", "S-3ASR": "Prospectus",
    "S-4": "Prospectus", "S-4MEF": "Prospectus", "S-11": "Prospectus",
    "F-1": "Prospectus", "F-3": "Prospectus", "F-4": "Prospectus",
    "424B1": "Prospectus", "424B2": "Prospectus", "424B3": "Prospectus",
    "424B4": "Prospectus", "424B5": "Prospectus", "424B7": "Prospectus",
    "FWP": "Prospectus",
    "SC TO-T": "Tender Offer", "SC TO-I": "Tender Offer", "SC 14D9": "Tender Offer",
    "SC 13E3": "Tender Offer",  # going-private transactions
}


# R1.htm, R2.htm, ... are EDGAR's auto-generated XBRL viewer pages, duplicating the financials.
XBRL_VIEWER_PAGE = re.compile(r"^R\d+\.html?$", re.I)


def form_group(form):
    return FORM_GROUPS.get(form.removesuffix("/A"))


class Edgar:
    def __init__(self, user_agent):
        if not re.search(r"\S+@\S+\.\S+", user_agent) or "example.com" in user_agent:
            sys.exit("SEC requires your email. Add it to the command, e.g.  --email jane@firm.com")
        self.user_agent = user_agent
        self._last = 0.0

    def get(self, url):
        # SEC's fair-access limit is 10 requests/second; stay well under it.
        for attempt in range(5):
            wait = 0.15 - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            req = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent, "Accept-Encoding": "gzip"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise
                if e.code in (429, 500, 502, 503) and attempt < 4:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 4:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise

    def get_json(self, url):
        return json.loads(self.get(url))

    def resolve(self, query):
        """Return (cik, display_name) for a ticker, CIK, or company name."""
        q = query.strip()
        if q.isdigit():
            return int(q), None
        tickers = self.get_json("https://www.sec.gov/files/company_tickers.json").values()
        for t in tickers:
            if t["ticker"].upper() == q.upper():
                return int(t["cik_str"]), t["title"]
        matches = [t for t in tickers if q.lower() in t["title"].lower()]
        if len(matches) == 1:
            return int(matches[0]["cik_str"]), matches[0]["title"]
        if not matches:
            # Delisted / taken-private companies are not in the ticker file;
            # fall back to EDGAR's company-name search.
            matches = self.search_name(q)
        if len(matches) == 1:
            return int(matches[0]["cik_str"]), matches[0]["title"]
        if not matches:
            sys.exit(f"No SEC registrant found for {q!r}. Try the ticker or CIK.")
        print(f"{q!r} matches several companies; re-run with the CIK:")
        for m in matches[:25]:
            print(f"  CIK {int(m['cik_str']):>10}  {m['title']}")
        sys.exit(1)

    def search_name(self, name):
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&output=atom"
               f"&count=40&company={urllib.parse.quote(name)}")
        root = ET.fromstring(self.get(url))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        out = []
        for entry in root.findall("a:entry", ns):
            content = entry.find("a:content", ns)
            cik = content.findtext("a:cik", default="", namespaces=ns) if content is not None else ""
            title = content.findtext("a:name", default="", namespaces=ns) if content is not None else ""
            if cik:
                out.append({"cik_str": cik, "title": title})
        # A single-company result comes back as a filings feed, not a list.
        if not out:
            cik = root.findtext("a:company-info/a:cik", namespaces=ns)
            title = root.findtext("a:company-info/a:conformed-name", namespaces=ns)
            if cik:
                out.append({"cik_str": cik, "title": title})
        return out

    def filings(self, cik):
        """Return (company_name, tickers, list of filing dicts), newest first."""
        sub = self.get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        pages = [sub["filings"]["recent"]]
        for f in sub["filings"].get("files", []):
            pages.append(self.get_json(f"https://data.sec.gov/submissions/{f['name']}"))
        rows = []
        for p in pages:
            for i in range(len(p["accessionNumber"])):
                rows.append({
                    "accession": p["accessionNumber"][i],
                    "date": p["filingDate"][i],
                    "report_date": p["reportDate"][i],
                    "form": p["form"][i],
                    "primary_doc": p["primaryDocument"][i],
                    "description": p["primaryDocDescription"][i],
                })
        rows.sort(key=lambda r: r["date"], reverse=True)
        self.last_submission = sub
        return sub["name"], sub.get("tickers", []), rows


class _TextExtractor(html.parser.HTMLParser):
    """Turn a filing's HTML into readable text, keeping each table row on one line."""

    BLOCK = {"p", "div", "br", "tr", "table", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5",
             "h6", "hr", "center", "blockquote", "pre", "section", "article", "dd", "dt"}
    SKIP = {"script", "style", "head", "title", "ix:header", "xbrl"}
    VOID = {"br", "hr", "img", "meta", "link", "input", "col", "area", "base", "wbr"}

    def __init__(self, table_markers=False):
        super().__init__(convert_charrefs=True)
        self.out, self.row, self.cell = [], None, None
        self.skip_tag, self.skip_depth = None, 0
        self.table_markers, self.table_depth = table_markers, 0

    def handle_starttag(self, tag, attrs):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        style = (dict(attrs).get("style") or "").replace(" ", "").lower()
        if tag not in self.VOID and (tag in self.SKIP or "display:none" in style):
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag == "table":
            self.table_depth += 1
            if self.table_markers and self.table_depth == 1:
                self.out.append(f"\n{TABLE_START}\n")
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag in self.BLOCK and self.cell is None:
            self.out.append("\n")
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            cells = self._merge_cells(self.row)
            if cells:
                self.out.append("\n" + " | ".join(cells))
            self.row = None
        elif tag in self.BLOCK and self.cell is None:
            self.out.append("\n")
        if tag == "table" and self.table_depth:
            self.table_depth -= 1
            if self.table_markers and self.table_depth == 0:
                self.out.append(f"\n{TABLE_END}\n")

    def handle_data(self, data):
        if self.skip_tag:
            return
        # Line breaks in HTML source are just spaces; real breaks come from tags.
        data = data.replace("\r", " ").replace("\n", " ")
        (self.cell if self.cell is not None else self.out).append(data)

    @staticmethod
    def _merge_cells(cells):
        # Financial tables put "$", ")" and "%" in their own cells; glue them to the number.
        merged = []
        for c in (c for c in cells if c):
            if merged and merged[-1] in ("$", "(", "$("):
                merged[-1] += c
            elif merged and c in (")", "%", ")%", "%)"):
                merged[-1] += c
            else:
                merged.append(c)
        return merged

    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.out).replace("\xa0", " ").split("\n"))
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


TABLE_START, TABLE_END = "\x01TABLE", "\x02TABLE"


def html_to_text(data, table_markers=False):
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:
        raw = data.decode("cp1252", errors="replace")
    parser = _TextExtractor(table_markers)
    parser.feed(raw)
    parser.close()
    return parser.text()


def write_text_copy(src, dest, company, filing, url):
    header = (f"Company: {company}\n"
              f"Form: {filing['form']}\n"
              f"Filed: {filing['date']}\n"
              + (f"Period of report: {filing['report_date']}\n" if filing["report_date"] else "")
              + f"Document: {url.rsplit('/', 1)[-1]}"
              + (" (main document)" if url.endswith("/" + filing["primary_doc"]) else " (exhibit)")
              + f"\nSEC accession no.: {filing['accession']}\nSource: {url}\n"
              + "=" * 70 + "\n\n")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(header + html_to_text(src.read_bytes()), encoding="utf-8")


def safe(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .")


def download_company(edgar, query, out_root, since=None, until=None, all_docs=False, dry_run=False,
                     label=None, text=False):
    cik, _ = edgar.resolve(query)
    name, tickers, rows = edgar.filings(cik)
    folder_name = safe(label or name) + (f" ({tickers[0]})" if tickers else "")
    company_dir = out_root / folder_name
    wanted = [r for r in rows if form_group(r["form"])
              and (not since or r["date"] >= since) and (not until or r["date"] <= until)]
    print(f"\n{name}  CIK {cik}  -> {company_dir}")
    print(f"  {len(wanted)} matching filings (of {len(rows)} total on EDGAR)")

    text_dir = company_dir / "Text for AI"
    index_rows, new, converted = [], 0, 0
    for r in wanted:
        acc = r["accession"].replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"
        group_dir = company_dir / form_group(r["form"])
        prefix = f"{r['date']}_{safe(r['form'].replace(' ', ''))}_{r['accession']}"
        # Older filings have no primary document; fall back to the full submission text.
        docs = [r["primary_doc"] or f"{r['accession']}.txt"]
        if all_docs and r["primary_doc"]:
            try:
                items = edgar.get_json(f"{base}/index.json")["directory"]["item"]
                docs = [i["name"] for i in items
                        if re.search(r"\.(htm|html|pdf|txt|xlsx?|jpg|gif|png)$", i["name"], re.I)
                        and not re.search(r"-index(-headers)?\.html?$|^\d{10}-\d{2}-\d{6}\.txt$",
                                          i["name"])
                        and not XBRL_VIEWER_PAGE.match(i["name"])]
            except urllib.error.HTTPError:
                pass
        for doc in docs:
            doc = doc.split("/")[-1]
            dest = group_dir / f"{prefix}_{safe(doc)}"
            index_rows.append({**r, "file": str(dest.relative_to(company_dir)),
                               "url": f"{base}/{doc}"})
            if dry_run:
                if not dest.exists():
                    print(f"  would download {r['date']} {r['form']:<9} {doc}")
                continue
            if not dest.exists():
                try:
                    data = edgar.get(f"{base}/{doc}")
                except urllib.error.HTTPError as e:
                    print(f"  ! {r['date']} {r['form']} {doc}: HTTP {e.code}")
                    continue
                group_dir.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                new += 1
                print(f"  + {r['date']} {r['form']:<9} {doc}")
            # Main documents and press releases (EX-99) go straight into "Text for AI", ready
            # to embed; contracts, certifications and other exhibits go in a subfolder.
            is_key = doc == r["primary_doc"] or re.search(r"ex-?99", doc, re.I)
            text_dest = (text_dir if is_key else text_dir / "Other exhibits") / (dest.stem + ".txt")
            if (text and dest.suffix.lower() in (".htm", ".html") and not text_dest.exists()
                    and not XBRL_VIEWER_PAGE.match(doc)):
                try:
                    write_text_copy(dest, text_dest, name, r, f"{base}/{doc}")
                    converted += 1
                except Exception as e:  # one bad file shouldn't stop the run
                    print(f"  ! could not convert {dest.name} to text: {e}")

    if not dry_run and index_rows:
        company_dir.mkdir(parents=True, exist_ok=True)
        with open(company_dir / "filings_index.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "form", "report_date", "description",
                                              "accession", "file", "url", "primary_doc"])
            w.writeheader()
            w.writerows(index_rows)
    print(f"  {new} new file(s) downloaded" + (" (dry run)" if dry_run else ""))
    if text and not dry_run:
        print(f"  {converted} text file(s) written to {text_dir}")


def main():
    cfg = dict(DEFAULT_CONFIG)
    if (HERE / "config.json").exists():
        cfg.update(json.loads((HERE / "config.json").read_text()))
    # Personal settings (e.g. user_agent email) go in config.local.json, which git ignores.
    local = HERE / "config.local.json"
    if local.exists():
        cfg.update(json.loads(local.read_text()))
    cfg["user_agent"] = os.environ.get("SEC_USER_AGENT", cfg["user_agent"])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("companies", nargs="*", help="ticker, CIK, or company name")
    ap.add_argument("--comps", metavar="SET", help="download every company in a set from comps.json")
    ap.add_argument("--since", default=cfg.get("since"), help="only filings on/after YYYY-MM-DD")
    ap.add_argument("--until", default=cfg.get("until"), help="only filings on/before YYYY-MM-DD")
    ap.add_argument("--out", default=cfg["output_dir"], help="output folder (default from config.json)")
    ap.add_argument("--all-docs", action="store_true",
                    help="also download exhibits (e.g. 8-K press releases in EX-99.1)")
    ap.add_argument("--text", action="store_true",
                    help="also save a clean .txt copy of each document in a 'Text for AI' folder")
    ap.add_argument("--email", help="your email, sent to SEC to identify you (SEC requires it)")
    ap.add_argument("--dry-run", action="store_true", help="list filings without downloading")
    args = ap.parse_args()

    targets = [(c, None) for c in args.companies]
    if args.comps:
        comps = json.loads((HERE / "comps.json").read_text())
        if args.comps not in comps:
            sys.exit(f"No comp set {args.comps!r} in comps.json")
        targets += [(c.get("cik") or c["ticker"], c.get("name")) for c in comps[args.comps]]
    if not targets:
        ap.error("give at least one company or --comps SET")

    out_root = Path(args.out).expanduser()
    if not args.dry_run and not out_root.parent.exists():
        sys.exit(f"Output folder's parent does not exist: {out_root.parent}")
    edgar = Edgar(f"{cfg['contact_name']} {args.email}" if args.email else cfg["user_agent"])
    for query, label in targets:
        download_company(edgar, query, out_root, args.since, args.until, args.all_docs, args.dry_run,
                         label, args.text)


# ---------------------------------------------------------------------------
# intake-filing: one filing -> per-item text files for a RAG workspace
# ---------------------------------------------------------------------------

VALUATION_DATE = "2020-10-23"

# Companies taken private drop out of SEC's ticker file; map them to their CIKs.
TICKER_ALIASES = {"ATSG": 894081, "AAWW": 1135185}

INTAKE_FORMS = {"10-K": "10K", "10-Q": "10Q", "8-K": "8K", "DEF 14A": "DEF14A"}

REQUIRED_ITEMS = {"10-K": ["Item1", "Item1A", "Item7", "Item8"],
                  "10-Q": ["PartI-Item1", "PartI-Item2"]}

# Item tables are extracted from these items (Item 15 only when Item 8 is a cross-reference).
TABLE_ITEMS = {"10-K": ["Item7", "Item8"], "10-Q": ["PartI-Item1", "PartI-Item2"]}

ITEM_HEADING = re.compile(
    r"^\s*item\s*(\d{1,2}[a-c]?)\b\s*([.:\-—–|][.:\-—–|\s]*)?(.*)$", re.I)
PART_HEADING = re.compile(r"^\s*part\s+(iv|iii|ii|i)\b\s*(?:[.:\-—–|]\s*)*(.*)$", re.I)
# A table-of-contents row ends in a page number: "Item 7. | Management's ... | 34"
TOC_ROW = re.compile(r"\|\s*(?:[A-Z]-)?\d{1,3}\s*$")


class CachedEdgar(Edgar):
    """Edgar client that keeps every response under raw/, so re-runs never refetch."""

    def __init__(self, user_agent, raw_dir, refresh=False):
        super().__init__(user_agent)
        self.raw_dir, self.refresh = raw_dir, refresh

    def _cache_path(self, url):
        u = urllib.parse.urlparse(url)
        path = u.path.lstrip("/") + (("__" + safe(u.query)) if u.query else "")
        return self.raw_dir / u.netloc / path

    def get(self, url):
        dest = self._cache_path(url)
        # Filing lists change over time; --refresh re-pulls them. Filing documents never change.
        volatile = "/submissions/" in url or "company_tickers" in url or "browse-edgar" in url
        if dest.exists() and not (self.refresh and volatile):
            return dest.read_bytes()
        data = super().get(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return data


def _date(s):
    import datetime
    return datetime.date.fromisoformat(s)


def _long_date(s):
    d = _date(s)
    return f"{d:%B} {d.day}, {d.year}"


def fiscal_year_bounds(fye_mmdd, fy):
    """(start, end) of fiscal year `fy`, labelled by the calendar year in which it ends."""
    import datetime
    month, day = int(fye_mmdd[:2]), int(fye_mmdd[2:])
    end = datetime.date(fy, month, min(day, 28 if month == 2 else day))
    start = datetime.date(fy - 1, month, end.day) + datetime.timedelta(days=1)
    return start, end


def resolve_filings(sub_rows, form, period, fye):
    """Pick the filing(s) for form + period. Returns (list of filing rows, period label)."""
    import datetime
    near = lambda a, b: abs((_date(a) - b).days) <= 10  # 52/53-week fiscal years drift a few days
    originals = [r for r in sub_rows if r["form"] == form]
    m_fy = re.fullmatch(r"FY(\d{4})", period, re.I)
    m_year = re.fullmatch(r"\d{4}", period)
    m_date = re.fullmatch(r"\d{4}-\d{2}-\d{2}", period)
    if not (m_fy or m_year or m_date):
        sys.exit(f"--period must be FY2020, 2020 or a date like 2020-05-31 (got {period!r})")

    if form == "10-K":
        if m_fy:
            _, end = fiscal_year_bounds(fye, int(m_fy.group(1)))
            label = f"FY{m_fy.group(1)}"
        elif m_date:
            end, label = _date(period), None
        else:
            _, end = fiscal_year_bounds(fye, int(period))
            label = f"FY{period}"
        hits = [r for r in originals if r["report_date"] and near(r["report_date"], end)]
        hits.sort(key=lambda r: r["date"])
        hits = hits[:1]  # the original annual report, not a later re-filing
        if hits and not label:
            label = f"FY{_date(hits[0]['report_date']).year}"
        return hits, label

    if form == "10-Q":
        if m_date:
            hits = [r for r in originals if r["report_date"] and near(r["report_date"], _date(period))]
        else:
            if m_fy:
                start, end = fiscal_year_bounds(fye, int(m_fy.group(1)))
            else:  # calendar year
                start, end = datetime.date(int(period), 1, 1), datetime.date(int(period), 12, 31)
            hits = [r for r in originals if r["report_date"]
                    and start <= _date(r["report_date"]) <= end]
        return sorted(hits, key=lambda r: r["report_date"]), None  # label per filing

    if form == "8-K":
        if m_date:
            hits = [r for r in originals if (r["report_date"] or r["date"]) == period]
        else:
            year = (m_fy or m_year).group(1) if m_fy else period
            hits = [r for r in originals if (r["report_date"] or r["date"]).startswith(year)]
        return sorted(hits, key=lambda r: (r["report_date"] or r["date"], r["date"])), None

    # DEF 14A: the proxy filed in that calendar year (or on that date)
    if m_date:
        hits = [r for r in originals if r["date"] == period]
    else:
        year = m_fy.group(1) if m_fy else period
        hits = [r for r in originals if r["date"].startswith(year)]
    return sorted(hits, key=lambda r: r["date"]), None


def fiscal_quarter(fye, report_date):
    """(fiscal year, quarter) for a quarter ending on report_date, per the company's fiscal year."""
    import datetime
    rd = _date(report_date)
    slack = datetime.timedelta(days=10)  # 52/53-week quarters end a few days off the month end
    for fy in (rd.year, rd.year + 1):
        start, end = fiscal_year_bounds(fye, fy)
        if start - slack <= rd <= end + slack:
            return fy, min(4, max(1, round(((rd - start).days + 1) / 91.3)))
    return rd.year, 0


def period_label(form, filing, fy_label, fye="1231"):
    if form == "10-K":
        return fy_label
    if form == "10-Q":
        # Calendar-year filers: 2020Q2. Others carry the fiscal year: FDX's Aug-2020 quarter is FY2021Q1.
        fy, q = fiscal_quarter(fye, filing["report_date"])
        return f"{fy}Q{q}" if fye == "1231" else f"FY{fy}Q{q}"
    if form == "8-K":
        return filing["report_date"] or filing["date"]
    return filing["date"][:4]


def period_phrase(form, filing, fy_label):
    if form == "10-K":
        return f"fiscal year ended {_long_date(filing['report_date'])}"
    if form == "10-Q":
        return f"quarter ended {_long_date(filing['report_date'])}"
    if form == "8-K":
        return f"event dated {_long_date(filing['report_date'] or filing['date'])}"
    return f"{filing['date'][:4]} annual meeting"


class _IndexPageParser(html.parser.HTMLParser):
    """Read the document table on an EDGAR filing index page: (href, type) per document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell, self.href = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row, self.href = [], None
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "a" and self.row is not None:
            self.href = self.href or dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.href and len(self.row) >= 4:
                self.rows.append((self.href.split("/")[-1].split("?")[0], self.row[3]))
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def filing_documents(edgar, cik, filing):
    acc = filing["accession"].replace("-", "")
    page = edgar.get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{filing['accession']}-index.htm")
    parser = _IndexPageParser()
    parser.feed(page.decode("utf-8", errors="replace"))
    return parser.rows


def split_items(text, form):
    """Split converted filing text into items. Returns (sections, item_map_lines).

    sections: list of (key, title, text); key "OtherItems" collects everything unmatched.
    """
    lines = text.split("\n")
    # 10-Q item numbers restart in Part II. Follow explicit PART headings when present; when an
    # item number drops without one (e.g. Item 6 -> Item 1), switch parts anyway.
    part, last_num, cands = None, None, []
    for i, line in enumerate(lines):
        plain = line.replace("|", " ").strip()
        mp = PART_HEADING.match(plain)
        if mp and len(plain) < 120:
            part, last_num = mp.group(1).upper(), None
            continue
        m = ITEM_HEADING.match(line)
        if not m or len(line) > 200:
            continue
        num = m.group(1).upper()
        if form == "10-Q":
            n = int(re.match(r"\d+", num).group())
            if part is None:
                part = "I"
            elif last_num is not None and n < last_num:
                part = "II" if part == "I" else "I"
            last_num = n
        # "Item 7. MD&A", "ITEM 7 - MD&A", "Item 7" alone, "Item 7 Management's..." are headings;
        # "Item 1A of this report discusses ..." is a sentence.
        if not m.group(2) and m.group(3) and not m.group(3)[0].isupper():
            continue
        title = m.group(3).replace("|", " ").strip()
        if not title:  # heading split across lines: take the next nonblank line as the title
            nxt = next((l for l in lines[i + 1:i + 4]
                        if l.strip() and l not in (TABLE_START, TABLE_END)), "")
            title = nxt.replace("|", " ").strip() if len(nxt) < 150 else ""
        title = TOC_ROW.sub("", title).strip(" .")
        key = (f"Part{part}-Item{num}" if form == "10-Q" and part else f"Item{num}")
        cands.append({"line": i, "key": key, "title": title,
                      "toc": bool(TOC_ROW.search(line))})

    # Measure each candidate's span to the next one; the table of contents and stray
    # cross-references give tiny spans, the real heading the longest.
    real = [c for c in cands if not c["toc"]]
    for j, c in enumerate(real):
        end = real[j + 1]["line"] if j + 1 < len(real) else len(lines)
        c["words"] = len(" ".join(lines[c["line"]:end]).split())
    best = {}
    for c in real:
        if c["key"] not in best or c["words"] > best[c["key"]]["words"]:
            best[c["key"]] = c
    kept = sorted(best.values(), key=lambda c: c["line"])
    for j, c in enumerate(kept):  # report the spans actually emitted
        end = kept[j + 1]["line"] if j + 1 < len(kept) else len(lines)
        c["words"] = len(strip_markers("\n".join(lines[c["line"]:end])).split())

    for c in kept:  # a heading laid out as a table: start the section at the table itself
        k = c["line"] - 1
        while k >= 0 and not lines[k].strip():
            k -= 1
        if k >= 0 and lines[k] == TABLE_START:
            c["line"] = k
    sections, other = [], []
    first = kept[0]["line"] if kept else len(lines)
    other.append("\n".join(lines[:first]))
    for j, c in enumerate(kept):
        end = kept[j + 1]["line"] if j + 1 < len(kept) else len(lines)
        sections.append((c["key"], c["title"], "\n".join(lines[c["line"]:end])))
    # Anything after the signatures / exhibit index stays with the last item; nothing is dropped.
    sections.append(("OtherItems", "Cover page, table of contents and unmatched text", "\n".join(other)))

    item_map = [f"  {'line':>6}  {'key':<16} {'words':>7}  title"]
    for c in kept:
        item_map.append(f"  {c['line']:>6}  {c['key']:<16} {c['words']:>7}  {c['title'][:70]}")
    skipped = [c for c in cands if c not in kept]
    if skipped:
        item_map.append(f"  ({len(skipped)} other heading match(es) treated as table of contents / "
                        f"cross-references, kept in surrounding text)")
    return sections, item_map


def extract_tables(section_text):
    """Return [(caption, rows_text)] for each table with 2+ rows and some numbers."""
    tables, lines, i = [], section_text.split("\n"), 0
    while i < len(lines):
        if lines[i] == TABLE_START:
            j = i + 1
            while j < len(lines) and lines[j] not in (TABLE_END, TABLE_START):
                j += 1
            rows = [l for l in lines[i + 1:j] if l.strip()]
            closed = j < len(lines) and lines[j] == TABLE_END
            if closed and len(rows) >= 2 and any(re.search(r"\d", r) for r in rows):
                caption = ""
                for k in range(i - 1, max(i - 15, -1), -1):
                    l = lines[k].strip()
                    if l and l not in (TABLE_START, TABLE_END) and "|" not in l:
                        caption = l[:200]
                        break
                tables.append((caption or "(no caption)", "\n".join(rows)))
            i = j + 1 if closed else j
        else:
            i += 1
    return tables


def strip_markers(text):
    t = "\n".join(l for l in text.split("\n") if l not in (TABLE_START, TABLE_END))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def tag_footer(ticker, form, period, item_label, filed, legal_name, phrase, accession, cite_item,
               available):
    return (f"\n\n--matter FILINGS --author {ticker} --type \"{form} {period} {item_label}\" "
            f"--date {filed[:4]}\n"
            f"--cite \"{legal_name}, Form {form} for {phrase}, filed {filed}, "
            f"EDGAR accession {accession}, {cite_item}\"\n"
            f"Available at {VALUATION_DATE}: {'YES' if available else 'NO'}\n"
            f"Source: EDGAR (digital text, no OCR)\n")


def item_cite(key, title):
    label = key.replace("PartI-", "Part I, ").replace("PartII-", "Part II, ") \
               .replace("Item", "Item ")
    return f"{label} ({title})" if title and key != "OtherItems" else (
        "cover page / unmatched text" if key == "OtherItems" else label)


def write_emitted(path, body, footer, emitted, meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = body.strip() or "(no text in this section)"  # a file must never begin with the tag block
    path.write_text(body + footer, encoding="utf-8")
    emitted.append({**meta, "file": path.name, "words": len(body.split())})


def update_index(out_dir, accession_rows):
    """Merge this run's rows into the manifest and regenerate filings_index.md."""
    manifest_path = out_dir / ".index.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    replaced = {r["accession"] for r in accession_rows}
    # Files deleted by hand (e.g. in Finder) drop out of the index on the next run.
    manifest = [r for r in manifest if r["accession"] not in replaced
                and (out_dir / r["file"]).exists()] + accession_rows
    manifest.sort(key=lambda r: (r["ticker"], r["filed"], r["file"]))
    manifest_path.write_text(json.dumps(manifest, indent=1))

    def table(rows):
        out = ["| Company | Form | Period | Item / table | Filed | Accession | Available at "
               f"{VALUATION_DATE} | File |", "|---|---|---|---|---|---|---|---|"]
        out += [f"| {r['ticker']} | {r['form']} | {r['period']} | {r['part']} | {r['filed']} | "
                f"{r['accession']} | {r['available']} | {r['file']} |" for r in rows]
        return out if rows else ["_(none)_"]

    yes = [r for r in manifest if r["available"] == "YES"]
    no = [r for r in manifest if r["available"] == "NO"]
    md = ["# Public filings index", "",
          f"Valuation date: {VALUATION_DATE}. Availability is the EDGAR filing date on or before "
          "that date. Regenerated on every intake-filing run.", "",
          f"## Contemporaneous record (on file by Oct 23, 2020) — {len(yes)} files", ""] + table(yes) + [
          "", f"## Post-valuation-date (hindsight/corroboration only) — {len(no)} files", ""] + table(no)
    (out_dir / "filings_index.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return manifest


class IntakeError(Exception):
    """A fetched filing produced nothing usable. Always reported, never skipped silently."""


def intake_one(edgar, cik, ticker, legal_name, form, filing, fy_label, out_dir, include_all, log,
               used_names, fye="1231"):
    acc_path = filing["accession"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_path}"
    period = period_label(form, filing, fy_label, fye)
    who = f"{ticker} {form} {period} filed {filing['date']} (accession {filing['accession']})"
    phrase = period_phrase(form, filing, fy_label)
    available = filing["date"] <= VALUATION_DATE
    stem = f"FILINGS_{ticker}_{INTAKE_FORMS[form]}-{period}"
    emitted = []
    common = {"ticker": ticker, "form": form, "period": period, "filed": filing["date"],
              "accession": filing["accession"], "available": "YES" if available else "NO"}

    def footer(item_label, cite):
        return tag_footer(ticker, form, period, item_label, filing["date"], legal_name, phrase,
                          filing["accession"], cite, available)

    if form == "8-K":
        docs = filing_documents(edgar, cik, filing)
        ex99 = [(d, t) for d, t in docs if re.match(r"EX-99", t, re.I)]
        if not ex99:
            if not include_all:
                log(f"    skip: no EX-99 exhibit (use --include-all to keep)")
                return []
            ex99 = [(filing["primary_doc"], "8-K")]
        for doc, typ in ex99:
            tag = re.sub(r"^EX-", "EX", typ.upper()).replace(".", "-") if typ != "8-K" else "Main"
            name, n = f"{stem}_{tag}", 2
            # Two 8-Ks with the same event date, or two exhibits of the same type, get _2, _3 ...
            while used_names.get(name, filing["accession"]) != filing["accession"] or \
                    name in used_names.get(("this", filing["accession"]), set()):
                name, n = f"{stem}_{tag}_{n}", n + 1
            used_names[name] = filing["accession"]
            used_names.setdefault(("this", filing["accession"]), set()).add(name)
            text = strip_markers(html_to_text(edgar.get(f"{base}/{doc}")))
            if not text.strip():
                raise IntakeError(f"{who}: exhibit {doc} ({typ}) converted to empty text")
            cite = f"Exhibit {typ.replace('EX-', '')}" if typ != "8-K" else "Form 8-K"
            write_emitted(out_dir / f"{name}.txt", text, footer(tag, cite), emitted,
                          {**common, "part": tag})
        return emitted

    text = html_to_text(edgar.get(f"{base}/{filing['primary_doc']}"), table_markers=True)
    if not strip_markers(text).strip():
        raise IntakeError(f"{who}: {filing['primary_doc']} converted to empty text")
    if form == "DEF 14A":
        write_emitted(out_dir / f"{stem}_Full.txt", strip_markers(text),
                      footer("Full", "Definitive Proxy Statement"), emitted, {**common, "part": "Full"})
        return emitted

    sections, item_map = split_items(text, form)
    log(f"    item map ({filing['primary_doc']}):")
    for line in item_map:
        log(line)
    keys = [k for k, _, _ in sections]
    if keys == ["OtherItems"]:
        raise IntakeError(f"{who}: zero items matched in {filing['primary_doc']}; nothing emitted")
    for req in REQUIRED_ITEMS[form]:
        if req not in keys:
            log(f"    WARNING: required {req} not detected; its text is in another item or OtherItems")

    table_items = list(TABLE_ITEMS[form])
    item8 = next((t for k, _, t in sections if k == "Item8"), "")
    if form == "10-K" and len(item8.split()) < 150 and "Item15" in keys:
        log("    NOTE: Item 8 is short (likely a cross-reference); also extracting Item 15 tables")
        table_items.append("Item15")

    for key, title, body in sections:
        write_emitted(out_dir / f"{stem}_{key}.txt", strip_markers(body),
                      footer(key, item_cite(key, title)), emitted, {**common, "part": key})
        if key in table_items:
            for n, (caption, rows) in enumerate(extract_tables(body), 1):
                tkey = f"{key}_T{n:02d}"
                write_emitted(out_dir / f"{stem}_{tkey}.txt", f"{caption}\n{rows}",
                              footer(tkey, f"{item_cite(key, title)}, table: {caption}"), emitted,
                              {**common, "part": tkey})
    return emitted


def load_intake_config():
    cfg = dict(DEFAULT_CONFIG)
    for f in ("config.json", "config.local.json"):
        if (HERE / f).exists():
            cfg.update(json.loads((HERE / f).read_text()))
    return cfg


def add_common_intake_args(ap, cfg):
    ap.add_argument("--email", help="contact email for the SEC User-Agent")
    ap.add_argument("--yes", action="store_true", help="don't ask to confirm non-December fiscal years")
    ap.add_argument("--dry-run", action="store_true", help="resolve and echo only; write nothing")
    ap.add_argument("--refresh", action="store_true", help="re-pull filing lists instead of using raw/")
    ap.add_argument("--out", default=cfg["intake_output_dir"],
                    help="output folder (default: the Western Global/SEC Filings Dropbox folder)")
    ap.add_argument("--raw", default=str(HERE / "raw"))


def open_edgar(args, cfg):
    ua = f"{cfg['contact_name']} {args.email}" if args.email else \
        os.environ.get("SEC_USER_AGENT", cfg["user_agent"])
    return CachedEdgar(ua, Path(args.raw).expanduser(), args.refresh)


def open_company(edgar, ticker=None, cik=None):
    if not cik:
        cik = TICKER_ALIASES.get(ticker.upper()) or edgar.resolve(ticker)[0]
    legal_name, tickers, rows = edgar.filings(cik)
    fye = edgar.last_submission.get("fiscalYearEnd") or "1231"
    ticker = (ticker or (tickers[0] if tickers else f"CIK{cik}")).upper()
    print(f"\n{legal_name}  CIK {cik}  ticker {ticker}  fiscal year end {fye[:2]}-{fye[2:]}")
    return {"cik": cik, "ticker": ticker, "legal_name": legal_name, "rows": rows, "fye": fye}


def echo_plan(company, form, hits, fy_label):
    for r in hits:
        avail = "YES" if r["date"] <= VALUATION_DATE else "NO"
        label = period_label(form, r, fy_label, company["fye"])
        print(f"  {form:<8} {label:<11} period end {r['report_date'] or '-':<10}  filed {r['date']}  "
              f"accession {r['accession']}  available at {VALUATION_DATE}: {avail}")


def confirm_fiscal_year(companies, args):
    odd = [c["ticker"] for c in companies if c["fye"] != "1231"]
    if not odd or args.yes or args.dry_run:
        return
    if not sys.stdin.isatty():
        sys.exit(f"Fiscal year does not end December 31 for {', '.join(odd)}; re-run with --yes.")
    if input(f"Fiscal year does not end December 31 for {', '.join(odd)}. "
             "Proceed with the filing(s) above? [y/N] ").strip().lower() != "y":
        sys.exit("Stopped.")


def prepare_output(args):
    out_dir = Path(args.out).expanduser()
    if not out_dir.parent.exists():
        sys.exit(f"Output folder's parent does not exist (is Dropbox running?): {out_dir.parent}")
    out_dir.mkdir(exist_ok=True)
    manifest_path = out_dir / ".index.json"
    used_names = {Path(m["file"]).stem: m["accession"]
                  for m in (json.loads(manifest_path.read_text()) if manifest_path.exists() else [])
                  if (out_dir / m["file"]).exists()}
    return out_dir, used_names


def run_jobs(edgar, jobs, out_dir, used_names, include_all, log):
    """jobs: [(company, form, hits, fy_label)]. Returns (emitted rows, error messages)."""
    emitted_all, errors = [], []
    for company, form, hits, fy_label in jobs:
        for r in hits:
            log(f"\n  {company['ticker']} {form} filed {r['date']} ({r['accession']}):")
            try:
                emitted = intake_one(edgar, company["cik"], company["ticker"], company["legal_name"],
                                     form, r, fy_label, out_dir, include_all, log, used_names,
                                     company["fye"])
            except IntakeError as e:
                log(f"    ERROR: {e}")
                errors.append(str(e))
                continue
            except urllib.error.URLError as e:  # includes HTTPError
                why = f"HTTP {e.code} {e.url}" if isinstance(e, urllib.error.HTTPError) else f"network: {e.reason}"
                msg = f"{company['ticker']} {form} filed {r['date']} ({r['accession']}): {why}"
                log(f"    ERROR: {msg}")
                errors.append(msg)
                continue
            emitted_all += emitted
            if emitted:
                log(f"    emitted {len(emitted)} file(s):")
                for e in emitted:
                    log(f"      {e['words']:>7} words  {e['file']}")
    return emitted_all, errors


def write_log(out_dir, name, lines):
    logs = out_dir / "logs"
    logs.mkdir(exist_ok=True)
    (logs / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def report_errors(errors):
    if errors:
        print(f"\n{len(errors)} ERROR(S) — these filings produced nothing and are NOT in the index:",
              file=sys.stderr)
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
    return 1 if errors else 0


def intake_main(argv):
    cfg = load_intake_config()
    ap = argparse.ArgumentParser(
        prog="sec_filings.py intake-filing",
        description="Fetch one filing (or a period's filings) and split it into per-item text "
                    "files with end-of-file tags for a RAG workspace.")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--ticker")
    who.add_argument("--cik", type=int)
    ap.add_argument("--form", required=True, choices=list(INTAKE_FORMS))
    ap.add_argument("--period", required=True,
                    help="FY2020 (fiscal year, labelled by the year it ends), 2020 (calendar year), "
                         "or a date: period end for 10-K/10-Q, event date for 8-K, filing date for DEF 14A")
    ap.add_argument("--include-all", action="store_true", help="8-K: keep filings with no EX-99")
    add_common_intake_args(ap, cfg)
    args = ap.parse_args(argv)

    edgar = open_edgar(args, cfg)
    company = open_company(edgar, args.ticker, args.cik)
    hits, fy_label = resolve_filings(company["rows"], args.form, args.period, company["fye"])
    if not hits:
        sys.exit(f"No {args.form} found for period {args.period}.")
    echo_plan(company, args.form, hits, fy_label)
    if args.form in ("10-K", "10-Q"):
        confirm_fiscal_year([company], args)
    if args.dry_run:
        return 0

    out_dir, used_names = prepare_output(args)
    log_lines = []

    def log(msg):
        print(msg)
        log_lines.append(msg)

    emitted, errors = run_jobs(edgar, [(company, args.form, hits, fy_label)], out_dir, used_names,
                               args.include_all, log)
    write_log(out_dir, f"{company['ticker']}_{INTAKE_FORMS[args.form]}_{args.period}.log", log_lines)
    update_index(out_dir, emitted)
    print(f"\nIndex: {out_dir / 'filings_index.md'}")
    return report_errors(errors)


# Batch scope for the Western Global comparable set. 10-K entries are fiscal-year END dates,
# so each company's own calendar is explicit. 10-Qs: "calendar:2020" = quarters ending in 2020;
# "filed:A:B" = 10-Qs filed between A and B (non-December fiscal years).
BATCH_SCOPE = {
    "ATSG": {"10-K": ["2019-12-31", "2020-12-31"], "10-Q": "calendar:2020"},
    "AAWW": {"10-K": ["2019-12-31", "2020-12-31"], "10-Q": "calendar:2020"},
    "UPS":  {"10-K": ["2019-12-31", "2020-12-31"], "10-Q": "calendar:2020"},
    "FDX":  {"10-K": ["2019-05-31", "2020-05-31"], "10-Q": "filed:2019-07-01:2021-01-31"},
    "AIRT": {"10-K": ["2020-03-31", "2021-03-31"], "10-Q": "filed:2019-07-01:2021-01-31"},
}
BATCH_8K_YEAR = "2020"


def plan_company(company, scope):
    jobs, rows = [], company["rows"]
    for end in scope["10-K"]:
        hits, label = resolve_filings(rows, "10-K", end, company["fye"])
        if not hits:
            print(f"  10-K     MISSING: no 10-K for fiscal year ended {end}")
        jobs.append((company, "10-K", hits, label))
    q = scope["10-Q"]
    if q.startswith("calendar:"):
        hits, _ = resolve_filings(rows, "10-Q", q.split(":")[1], company["fye"])
    else:
        _, lo, hi = q.split(":")
        hits = sorted((r for r in rows if r["form"] == "10-Q" and lo <= r["date"] <= hi),
                      key=lambda r: r["report_date"])
    jobs.append((company, "10-Q", hits, None))
    hits, _ = resolve_filings(rows, "8-K", BATCH_8K_YEAR, company["fye"])
    jobs.append((company, "8-K", hits, None))
    for _, form, hits, label in jobs:
        echo_plan(company, form, hits, label)
    return jobs


def batch_report(out_dir, tickers, errors):
    manifest = json.loads((out_dir / ".index.json").read_text()) if (out_dir / ".index.json").exists() else []
    manifest = [m for m in manifest if m["ticker"] in tickers]
    print("\n" + "=" * 78 + "\nFINAL INDEX SUMMARY (this batch's companies)\n" + "=" * 78)
    forms = list(INTAKE_FORMS)
    print(f"{'Company':<8}" + "".join(f"{f:>10}" for f in forms) + f"{'YES':>8}{'NO':>8}   filings (YES/NO)")
    zero_yes = []
    for t in tickers:
        mine = [m for m in manifest if m["ticker"] == t]
        counts = "".join(f"{sum(1 for m in mine if m['form'] == f):>10}" for f in forms)
        yes = sum(1 for m in mine if m["available"] == "YES")
        no = len(mine) - yes
        fy = len({m["accession"] for m in mine if m["available"] == "YES"})
        fn = len({m["accession"] for m in mine if m["available"] == "NO"})
        print(f"{t:<8}{counts}{yes:>8}{no:>8}   {fy}/{fn}")
        if yes == 0:
            zero_yes.append(t)
    for title, flag in (("Contemporaneous record (on file by Oct 23, 2020)", "YES"),
                        ("Post-valuation-date (hindsight/corroboration only)", "NO")):
        rows = [m for m in manifest if m["available"] == flag]
        filings = {}
        for m in rows:
            filings.setdefault((m["ticker"], m["form"], m["period"], m["filed"], m["accession"]), 0)
            filings[(m["ticker"], m["form"], m["period"], m["filed"], m["accession"])] += 1
        print(f"\n## {title}: {len(filings)} filings, {len(rows)} files")
        for (t, f, per, filed, acc), n in sorted(filings.items()):
            print(f"  {t:<5} {f:<8} {per:<12} filed {filed}  {acc}  {n:>3} files")
    if zero_yes:
        print(f"\nWARNING: companies with ZERO contemporaneous (YES) files: {', '.join(zero_yes)}")
    return report_errors(errors)


def intake_batch_main(argv):
    cfg = load_intake_config()
    ap = argparse.ArgumentParser(
        prog="sec_filings.py intake-batch",
        description="Run intake-filing over the comparable set: 10-K FY2019/FY2020 (per each "
                    "company's fiscal calendar), 2020 10-Qs, and 2020 8-K EX-99 releases.")
    ap.add_argument("--tickers", nargs="+", default=list(BATCH_SCOPE), choices=list(BATCH_SCOPE))
    add_common_intake_args(ap, cfg)
    args = ap.parse_args(argv)

    edgar = open_edgar(args, cfg)
    companies, jobs = [], []
    print("PLAN (nothing downloaded yet except filing lists):")
    for t in args.tickers:
        c = open_company(edgar, t)
        companies.append(c)
        jobs += plan_company(c, BATCH_SCOPE[t])
    confirm_fiscal_year(companies, args)
    if args.dry_run:
        return 0

    out_dir, used_names = prepare_output(args)
    log_lines = []

    def log(msg):
        print(msg)
        log_lines.append(msg)

    emitted, errors = run_jobs(edgar, jobs, out_dir, used_names, include_all=False, log=log)
    write_log(out_dir, "batch.log", log_lines + [f"ERROR: {e}" for e in errors])
    update_index(out_dir, emitted)  # regenerated once, at the end
    print(f"\nIndex: {out_dir / 'filings_index.md'}")
    return batch_report(out_dir, [c["ticker"] for c in companies], errors)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "intake-filing":
        sys.exit(intake_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "intake-batch":
        sys.exit(intake_batch_main(sys.argv[2:]))
    main()
