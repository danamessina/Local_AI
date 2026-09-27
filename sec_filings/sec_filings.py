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
    "output_dir": "/Users/danamessina/KM Server Dropbox/KM Server Team Folder/Consulting/DOL/2025/"
                  "Western Global/SEC Filings",
    "since": None,
    "until": None,
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
        return sub["name"], sub.get("tickers", []), rows


class _TextExtractor(html.parser.HTMLParser):
    """Turn a filing's HTML into readable text, keeping each table row on one line."""

    BLOCK = {"p", "div", "br", "tr", "table", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5",
             "h6", "hr", "center", "blockquote", "pre", "section", "article", "dd", "dt"}
    SKIP = {"script", "style", "head", "title", "ix:header", "xbrl"}
    VOID = {"br", "hr", "img", "meta", "link", "input", "col", "area", "base", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.row, self.cell = [], None, None
        self.skip_tag, self.skip_depth = None, 0

    def handle_starttag(self, tag, attrs):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        style = (dict(attrs).get("style") or "").replace(" ", "").lower()
        if tag not in self.VOID and (tag in self.SKIP or "display:none" in style):
            self.skip_tag, self.skip_depth = tag, 1
            return
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


def html_to_text(data):
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:
        raw = data.decode("cp1252", errors="replace")
    parser = _TextExtractor()
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
    edgar = Edgar(f"SEC filings research {args.email}" if args.email else cfg["user_agent"])
    for query, label in targets:
        download_company(edgar, query, out_root, args.since, args.until, args.all_docs, args.dry_run,
                         label, args.text)


if __name__ == "__main__":
    main()
